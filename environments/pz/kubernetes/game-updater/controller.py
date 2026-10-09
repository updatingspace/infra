#!/usr/bin/env python3
"""Update public Steam builds only after a verified, gracefully stopped snapshot."""
import argparse
import copy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

BACKUP = Path('/opt/pz-backup/coordinator.py')
if not BACKUP.exists():
    BACKUP = Path(__file__).resolve().parent.parent / 'backup/coordinator.py'
spec = importlib.util.spec_from_file_location('backup_coordinator', BACKUP)
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)
APP = '380870'
LABEL = 'game-update'
STATUS = b.STATE / 'game-update.json'
WORKER = '''import fcntl, os, subprocess, sys
with open('/pz-server/.runtime.lock', 'a') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    env = {k:v for k,v in os.environ.items() if k != 'JAVA_TOOL_OPTIONS'}
    sys.exit(subprocess.call(['/home/steam/steamcmd/steamcmd.sh',
        '+@ShutdownOnFailedCommand', '1', '+@NoPromptForPassword', '1',
        '+force_install_dir', '/pz-server', '+login', 'anonymous',
        '+app_update', '380870', '-beta', 'public', '+quit'], env=env))
'''


def vdf(text, root):
    """Extract one balanced Valve KeyValues document; reject ambiguous keys."""
    text = re.sub(r'\x1b\[[0-9;]*m', '', text)
    match = re.search(r'"' + re.escape(root) + r'"\s*\{', text)
    b.require(match is not None, 'steam_document_missing')
    tokens = iter(re.findall(r'"((?:\\.|[^"\\])*)"|([{}])', text[match.start():]))

    def token():
        try:
            quoted, brace = next(tokens)
            return brace or quoted
        except StopIteration:
            raise b.Refused('steam_document_truncated') from None

    def obj():
        result = {}
        while True:
            key = token()
            if key == '}':
                return result
            b.require(key != '{' and key not in result, 'steam_document_ambiguous')
            value = token()
            b.require(value != '}', 'steam_document_invalid')
            result[key] = obj() if value == '{' else value

    b.require(token() == root and token() == '{', 'steam_document_invalid')
    return obj()


def build_id(value):
    b.require(isinstance(value, str) and re.fullmatch(r'[1-9][0-9]{0,19}', value), 'steam_build_invalid')
    return value


def installed():
    data = vdf((b.DATA / 'pz-server/steamapps/appmanifest_380870.acf').read_text(), 'AppState')
    b.require(data.get('appid') == APP and data.get('StateFlags') == '4', 'installed_app_not_complete')
    b.require(data.get('UserConfig', {}).get('BetaKey', 'public') in ('', 'public'), 'installed_branch_not_public')
    return build_id(data.get('buildid'))


def kubectl(args, timeout=60, data=None):
    try:
        result = subprocess.run([b.K3S, 'kubectl', '-n', 'zomboid', *args], input=data,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        raise b.Refused('game_update_command_timed_out') from None
    b.require(result.returncode == 0, 'game_update_command_failed')
    return result.stdout


def players():
    output = kubectl(['exec', 'zomboid-0', '--', 'timeout', '30', 'python3',
                      '/usr/local/bin/pz-game', 'rcon', 'players'], timeout=40).decode()
    matches = re.findall(r'Players connected \((\d+)\)', output)
    b.require(len(matches) == 1, 'player_count_unavailable')
    return int(matches[0])


def latest():
    output = kubectl(['exec', 'zomboid-0', '--', 'timeout', '600',
                      '/home/steam/steamcmd/steamcmd.sh', '+login', 'anonymous',
                      '+app_info_update', '1', '+app_info_print', APP, '+quit'], timeout=620)
    try:
        return build_id(vdf(output.decode(), APP)['depots']['branches']['public']['buildid'])
    except (KeyError, TypeError, UnicodeError):
        raise b.Refused('public_build_unavailable') from None


def status(outcome, **fields):
    value = {'checked_at': b.utc(), 'outcome': outcome, **fields}
    b.atomic_json(STATUS, value)
    print(json.dumps(value), flush=True)


def job_document(original, name):
    pod = original['spec']['template']['spec']
    container = pod['containers'][0]
    b.require(len(pod['containers']) == 1 and container['name'] == 'zomboid', 'game_template_unexpected')
    mounts = [copy.deepcopy(m) for m in container['volumeMounts'] if m['name'] in {'pz-server', 'steam'}]
    volumes = [copy.deepcopy(v) for v in pod['volumes'] if v['name'] in {'pz-server', 'steam'}]
    b.require({m['name'] for m in mounts} == {'pz-server', 'steam'}
              and {v['name'] for v in volumes} == {'pz-server', 'steam'}
              and all('persistentVolumeClaim' in v for v in volumes), 'game_volumes_unexpected')
    return {'apiVersion': 'batch/v1', 'kind': 'Job',
            'metadata': {'name': name, 'namespace': 'zomboid', 'labels': {'app.kubernetes.io/name': LABEL}},
            'spec': {'backoffLimit': 0, 'activeDeadlineSeconds': 1800,
                     'template': {'metadata': {'labels': {'app.kubernetes.io/name': LABEL}}, 'spec': {
                         'restartPolicy': 'Never', 'automountServiceAccountToken': False,
                         'terminationGracePeriodSeconds': 60, 'nodeSelector': pod.get('nodeSelector', {}),
                         'securityContext': copy.deepcopy(pod['securityContext']), 'volumes': volumes,
                         'containers': [{'name': 'update', 'image': container['image'], 'imagePullPolicy': 'Never',
                                         'command': ['python3', '-c', WORKER], 'volumeMounts': mounts,
                                         'securityContext': {'allowPrivilegeEscalation': False,
                                                             'capabilities': {'drop': ['ALL']}},
                                         'resources': {'requests': {'cpu': '500m', 'memory': '1Gi', 'ephemeral-storage': '512Mi'},
                                                       'limits': {'cpu': '1800m', 'memory': '2Gi', 'ephemeral-storage': '2Gi'}}}]}}}}


class Update(b.Coordinator):
    def __init__(self, cfg, target, force=False):
        super().__init__(cfg)
        self.target, self.force, self.mutating = target, force, False

    def phase(self, phase, **fields):
        if self.mutating and phase == 'apps_restoring':
            phase = 'game_update_starting'
        return super().phase(phase, **fields)

    def stop_collector(self):
        # Recheck after updater suspension/recovery preparation, immediately
        # before the existing save/quit stop sequence. Never parse player names.
        b.require(self.force or players() == 0, 'players_joined_defer_update')
        return super().stop_collector()

    def perform_update(self):
        name = 'game-update-' + self.journal['snapshot_id'].lower()
        self.phase('game_update_preparing', game_update={'target': self.target, 'job': name, 'verified': False})
        b.require(not self.api.pods('zomboid') and not self.api.pods('panel'), 'writers_running_before_update')
        b.require(not self.api.pods(LABEL), 'previous_update_pod_requires_inspection')
        self.api.no_unknown_writers()
        self.phase('game_updating')
        document = job_document(self.journal['original']['zomboid'], name)
        job = json.loads(kubectl(['create', '-f', '-', '-o', 'json'], data=json.dumps(document).encode()))
        uid = job['metadata']['uid']
        deadline = time.monotonic() + 1920
        while True:
            current = self.api.get('job', name)
            b.require(current['metadata']['uid'] == uid, 'update_job_identity_changed')
            conditions = current.get('status', {}).get('conditions', [])
            b.require(not any(c['type'] == 'Failed' and c['status'] == 'True' for c in conditions), 'steam_update_failed')
            if any(c['type'] == 'Complete' and c['status'] == 'True' for c in conditions):
                break
            b.require(time.monotonic() < deadline, 'steam_update_deadline_exceeded')
            time.sleep(5)
        pods = self.api.pods(LABEL)
        b.require(len(pods) == 1 and pods[0]['status']['phase'] == 'Succeeded'
                  and any(o.get('uid') == uid for o in pods[0]['metadata'].get('ownerReferences', [])), 'update_pod_not_quiescent')
        exits = pods[0]['status'].get('containerStatuses', [])
        b.require(len(exits) == 1 and exits[0].get('state', {}).get('terminated', {}).get('exitCode') == 0,
                  'update_exit_not_clean')
        logs = kubectl(['logs', 'job/' + name], timeout=60).decode(errors='replace')
        b.require("Success! App '380870' fully installed." in logs, 'steam_success_marker_missing')
        b.require(installed() == self.target, 'installed_build_mismatch')
        # Completion and runtime lock both prove the mutating process is gone.
        with b.locked(b.DATA / 'pz-server/.runtime.lock'):
            self.journal['game_update']['verified'] = True
            self.phase('game_update_verified')
        kubectl(['delete', 'job', name, '--wait=true', '--timeout=90s'], timeout=100)
        b.wait_for(lambda: not self.api.pods(LABEL), 90, 'update_pod_cleanup_timed_out')

    def restore_apps(self):
        if self.journal['phase'] != 'staging_verified':
            # Failed staging has not touched game binaries; restore old game.
            return super().restore_apps()
        self.mutating = True
        try:
            self.perform_update()
            b.GAME_READY_WAIT_SECONDS = 1800
            now = datetime.now(timezone.utc)
            self.journal['game_ready_wait'] = {'started_at': now.isoformat(),
                'deadline_at': (now + timedelta(seconds=b.GAME_READY_WAIT_SECONDS)).isoformat(), 'result': 'pending'}
            self.phase('game_update_starting')
            self.restore_replica('zomboid')
            self.wait_restored_game()
            b.require(self.journal['game_ready_wait']['result'] == 'ready', 'updated_game_not_ready')
            players()  # Fresh authenticated RCON health after readiness.
            self.restore_replica('panel')
            self.restore_replica('otel-collector')
            self.restore_updater()
            self.phase('apps_restored', downtime_finished_at=b.utc())
        except BaseException:
            self.phase('game_update_failed')
            # Keep the verified old snapshot and pause further maintenance.
            # Never overwrite a world that may already have been migrated.
            self.restore_replica('otel-collector')
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--force-players', action='store_true', help='Operator-authorized immediate maintenance')
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    b.require(os.geteuid() == 0, 'root_required')
    cfg = b.configuration(Path('/etc/pz-backup/config.json'))
    with b.locked(b.LOCK, create=True):
        b.disk_migration_ready()
        previous = b.read_json(b.STATE / 'journal.json')
        b.require(previous.get('phase') in b.TERMINAL, 'unfinished_backup_or_update_requires_inspection')
        update = Update(cfg, None, args.force_players)
        original = update.original_state()
        b.require(all(original[n]['spec']['replicas'] == 1 for n in ('zomboid', 'panel', 'otel-collector')),
                  'running_stack_required')
        b.require(not update.api.pods(LABEL), 'previous_update_requires_inspection')
        current, target = installed(), latest()
        count = players()
        if current == target:
            return status('current', installed=current, available=target)
        if int(target) < int(current):
            return status('deferred_older_public_build', installed=current, available=target)
        if args.check or (count and not args.force_players):
            return status('available' if args.check else 'deferred_players', installed=current, available=target, players=count)
        status('updating', installed=current, available=target)
        update.target = target
        update.run()
        status('updated', installed=installed(), available=target, snapshot_id=update.journal['snapshot_id'])
    b.command(['systemctl', 'start', '--no-block', 'pz-backup-upload.service'])


if __name__ == '__main__':
    try:
        main()
    except (b.Refused, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        reason = str(error) if isinstance(error, b.Refused) else type(error).__name__
        if reason == 'maintenance_lock_busy':
            print(json.dumps({'outcome': 'deferred_maintenance'}), flush=True)
        else:
            status('failed', reason=reason)
            sys.exit(1)
