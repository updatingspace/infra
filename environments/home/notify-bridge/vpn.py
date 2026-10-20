#!/usr/bin/env python3
"""Maintain the Telegram-only egress from a private VLESS subscription.
No subscription URLs, UUIDs, provider names or raw subprocess errors are logged.
"""
import argparse
import base64
import contextlib
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import uuid

ROOT = Path('/opt/notify-bridge/private')
XRAY = '/opt/notify-bridge/bin/xray'
KUBE = ['k3s', 'kubectl', '--cache-dir=/opt/notify-bridge/private/kube-cache']


def atomic(path, data):
    path = Path(path)
    temp = path.with_suffix(path.suffix + '.tmp')
    with open(temp, 'w', encoding='utf-8') as f:
        os.chmod(temp, 0o600)
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)


def nodes(raw):
    text = raw.decode().strip()
    if not text.startswith('vless://'):
        text = base64.b64decode(text + '=' * (-len(text) % 4), validate=True).decode()
    result = []
    for line in text.splitlines()[:100]:
        try:
            u = urllib.parse.urlsplit(line.strip())
            q = urllib.parse.parse_qs(u.query, keep_blank_values=True)
            get = lambda key, default='': q.get(key, [default])[0]
            # This deployment intentionally supports only encrypted XHTTP on 443.
            if u.scheme != 'vless' or get('type') != 'xhttp' or get('security') not in ('tls', 'reality') or u.port != 443:
                continue
            user_id = str(uuid.UUID(u.username))
            if not u.hostname or len(u.hostname) > 253:
                continue
            stream = {'network': 'xhttp', 'security': get('security')}
            xhttp = {'host': get('host'), 'path': get('path', '/'), 'mode': get('mode', 'auto')}
            extra = json.loads(get('extra', '{}'))
            allowed = {'noGRPCHeader', 'noSSEHeader', 'xPaddingBytes', 'xmux'}
            if set(extra) - allowed or not isinstance(extra, dict):
                continue
            if 'xmux' in extra and set(extra['xmux']) - {'hKeepAlivePeriod', 'maxConnections', 'maxConcurrency', 'cMaxReuseTimes', 'hMaxRequestTimes', 'hMaxReusableSecs'}:
                continue
            xhttp['extra'] = extra
            stream['xhttpSettings'] = xhttp
            tls = {'serverName': get('sni') or u.hostname, 'fingerprint': get('fp', 'chrome')}
            if get('security') == 'tls':
                tls['allowInsecure'] = False
                if get('alpn'):
                    tls['alpn'] = get('alpn').split(',')
                stream['tlsSettings'] = tls
            else:
                if not get('pbk'):
                    continue
                tls.update({'publicKey': get('pbk'), 'shortId': get('sid')})
                stream['realitySettings'] = tls
            outbound = {'tag': 'vpn', 'protocol': 'vless', 'settings': {'vnext': [{'address': u.hostname, 'port': u.port, 'users': [{'id': user_id, 'encryption': 'none', 'flow': get('flow')}]}]}, 'streamSettings': stream}
            result.append(outbound)
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
    if not result:
        raise ValueError('no supported encrypted nodes')
    return result


def config(node, port=1081):
    return {'log': {'loglevel': 'none'},
            'inbounds': [{'listen': '127.0.0.1', 'port': port, 'protocol': 'socks', 'settings': {'auth': 'noauth', 'udp': False}}],
            'outbounds': [node, {'tag': 'block', 'protocol': 'blackhole'}],
            'routing': {'domainStrategy': 'AsIs', 'rules': [
                {'type': 'field', 'domain': ['full:api.telegram.org'], 'port': '443', 'network': 'tcp', 'outboundTag': 'vpn'},
                {'type': 'field', 'network': 'tcp,udp', 'outboundTag': 'block'}]}}


def probe(address):
    r = subprocess.run(['curl', '-sS', '--proxy', 'socks5h://' + address, '--connect-timeout', '5', '--max-time', '12', '--output', '/dev/null', '--write-out', '%{http_code}', 'https://api.telegram.org'], capture_output=True, timeout=15)
    return r.returncode == 0 and r.stdout.strip() in (b'200', b'302')


def node_hash(node):
    return hashlib.sha256(json.dumps(node, sort_keys=True).encode()).hexdigest()


def test_node(node):
    address = node['settings']['vnext'][0]['address']
    # Subscription content cannot select loopback, LAN, metadata or cluster endpoints.
    addresses = socket.getaddrinfo(address, 443, family=socket.AF_INET, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        return False
    candidate = ROOT / 'candidate.json'
    atomic(candidate, json.dumps(config(node, 18081)))
    valid = subprocess.run([XRAY, 'run', '-test', '-config', str(candidate)], capture_output=True, timeout=10)
    if valid.returncode:
        return False
    proc = subprocess.Popen([XRAY, 'run', '-config', str(candidate)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(30):
            if proc.poll() is not None:
                return False
            try:
                with socket.create_connection(('127.0.0.1', 18081), timeout=.2):
                    break
            except OSError:
                time.sleep(.1)
        return probe('127.0.0.1:18081')
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        candidate.unlink(missing_ok=True)


def kube(*args, data=None):
    r = subprocess.run(KUBE + list(args), input=data, capture_output=True, text=True, timeout=60)
    if r.returncode:
        raise RuntimeError('kubernetes operation failed')
    return r.stdout


def apply_config(data):
    obj = {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': 'telegram-egress-config', 'namespace': 'notifications'}, 'type': 'Opaque', 'stringData': {'config.json': data}}
    kube('apply', '-f', '-', data=json.dumps(obj))
    digest = hashlib.sha256(data.encode()).hexdigest()
    patch = {'spec': {'template': {'metadata': {'annotations': {'notify.updspace.com/config': digest}}}}}
    kube('-n', 'notifications', 'patch', 'deployment', 'telegram-egress', '--type=merge', '-p', json.dumps(patch))
    kube('-n', 'notifications', 'rollout', 'status', 'deployment/telegram-egress', '--timeout=50s')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--bootstrap', action='store_true')
    parser.add_argument('--force-refresh', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    ROOT.mkdir(parents=True, exist_ok=True)
    import fcntl
    with open(ROOT / 'vpn.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state_path = ROOT / 'vpn-state.json'
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        now = int(time.time())
        changed = False
        refresh_due = now - state.get('last_refresh', 0) > 21600 or state.get('probe_failures', 0) >= 2
        if args.force_refresh or (refresh_due and now - state.get('last_refresh_attempt', 0) >= 300):
            state['last_refresh_attempt'] = now
            try:
                url = (ROOT / 'subscription.url').read_text().strip()
                if urllib.parse.urlsplit(url).scheme != 'https':
                    raise ValueError('subscription must use HTTPS')
                req = urllib.request.Request(url, headers={'User-Agent': 'notify-bridge/1.0'})
                with urllib.request.urlopen(req, timeout=20) as response:
                    raw = response.read(4 * 1024 * 1024 + 1)
                if len(raw) > 4 * 1024 * 1024:
                    raise ValueError('oversized subscription')
                nodes(raw)
                old = (ROOT / 'subscription.raw').read_bytes() if (ROOT / 'subscription.raw').exists() else b''
                changed = raw != old
                atomic(ROOT / 'subscription.raw', raw.decode())
                state['last_refresh'] = now
                state['refresh_errors'] = 0
            except Exception:
                state['refresh_errors'] = state.get('refresh_errors', 0) + 1
                print('subscription refresh failed; retaining last validated configuration', flush=True)
        healthy = False
        if not args.bootstrap:
            try:
                address = kube('-n', 'notifications', 'get', 'svc', 'telegram-egress', '-o', 'jsonpath={.spec.clusterIP}').strip() + ':1080'
                healthy = probe(address)
            except Exception:
                healthy = False
        if healthy:
            state['last_probe_success'] = now
            state['probe_failures'] = 0
        else:
            state['probe_failures'] = state.get('probe_failures', 0) + 1
        available = nodes((ROOT / 'subscription.raw').read_bytes())
        # Changing credentials in the feed must eventually replace an otherwise still-working old connection.
        current_present = any(node_hash(n) == state.get('selected') for n in available)
        if not args.bootstrap and healthy and current_present:
            atomic(state_path, json.dumps(state))
            return
        if not args.bootstrap and not changed and state['probe_failures'] < 3 and current_present:
            atomic(state_path, json.dumps(state))
            return
        available.sort(key=lambda n: node_hash(n) != state.get('selected'))
        for index, node in enumerate(available):
            try:
                success = test_node(node)
            except Exception:
                success = False
            print('candidate %d/%d telegram_tls=%s' % (index + 1, len(available), success), flush=True)
            if not success:
                continue
            data = json.dumps(config(node), indent=2)
            target = ROOT / 'xray.json'
            previous = target.read_text() if target.exists() else None
            if previous:
                atomic(ROOT / 'xray.previous.json', previous)
            if not args.bootstrap:
                try:
                    apply_config(data)
                    if not probe(address):
                        raise RuntimeError('post-rollout probe failed')
                except Exception:
                    if previous:
                        apply_config(previous)
                    raise RuntimeError('new egress failed; rollback attempted')
            atomic(target, data)
            state.update(selected=node_hash(node), last_probe_success=now, probe_failures=0, last_switch=now)
            atomic(state_path, json.dumps(state))
            print('validated egress selected', flush=True)
            return
        atomic(state_path, json.dumps(state))
        raise RuntimeError('no candidate can reach Telegram; existing configuration retained')

if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('VPN maintenance failed: ' + type(exc).__name__, file=sys.stderr)
        sys.exit(1)
