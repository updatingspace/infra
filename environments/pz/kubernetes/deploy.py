#!/usr/bin/env python3
"""Sync source and run reviewed Terraform plans on the VM; no credential export."""
import argparse
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import tarfile
import time

ROOT = Path(__file__).resolve().parent
STAGES = ("platform", "workloads", "observability")
REMOTE_ROOT = "/opt/pz-infrastructure"
APPLY_TIMEOUT = 20 * 60
UPDATER_PAUSE_TIMEOUT = 15 * 60
TELEMETRY_MODULES = ("preload.mjs", "provider.mjs", "server-transform.mjs", "client-overlay.mjs")

# Shared by the pause observer and the final apply gate. Only fixed summaries
# cross SSH; API responses and the panel journal never leave the VM.
UPDATER_GUARD = r'''
import json
import os
from pathlib import Path
import stat
import subprocess

class UpdaterUnavailable(Exception):
    pass

def updater_kubectl(arguments):
    try:
        result = subprocess.run([
            "k3s", "kubectl", "--kubeconfig", "/etc/rancher/k3s/operator.yaml",
            "--request-timeout=10s", "-n", "zomboid", *arguments,
        ], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        raise UpdaterUnavailable() from None
    if result.returncode:
        raise UpdaterUnavailable()
    try:
        return json.loads(result.stdout) if result.stdout.strip() else None
    except (ValueError, UnicodeError):
        raise UpdaterUnavailable() from None

def updater_journal_state():
    journal = Path("/opt/pz-stack/data/panel/.k8s-panel-updater/journal.json")
    try:
        descriptor = os.open(journal, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "invalid"
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 8 * 1024 ** 2:
            return "invalid"
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            document = json.load(source)
        if not isinstance(document, dict):
            return "invalid"
        return "terminal" if document.get("phase") in {"committed", "rolled_back"} else "incomplete"
    except (OSError, ValueError, UnicodeError):
        return "invalid"
    finally:
        os.close(descriptor)

def updater_snapshot():
    cron = updater_kubectl(["get", "cronjob", "panel-auto-update", "--ignore-not-found", "-o", "json"])
    selection = ["-l", "app.kubernetes.io/name=panel-auto-update", "-o", "json"]
    jobs = updater_kubectl(["get", "jobs", *selection])["items"]
    pods = updater_kubectl(["get", "pods", *selection])["items"]
    # A newly created Job may have no .status.active yet; terminal conditions
    # are the only proof that it cannot still start a writer.
    active_jobs = sum(not any(condition.get("type") in {"Complete", "Failed"}
                             and condition.get("status") == "True"
                             for condition in job.get("status", {}).get("conditions", [])) for job in jobs)
    active_pods = sum(bool(pod.get("metadata", {}).get("deletionTimestamp"))
                      or pod.get("status", {}).get("phase") not in {"Succeeded", "Failed"} for pod in pods)
    return {"status": "ok", "suspended": cron is None or cron.get("spec", {}).get("suspend") is True,
            "active_jobs": active_jobs, "active_pods": active_pods, "journal": updater_journal_state()}

def require_updater_idle():
    state = updater_snapshot()
    if not state["suspended"]:
        raise RuntimeError("Panel updater is not suspended; run the workloads plan preflight first")
    if state["active_jobs"] or state["active_pods"]:
        raise RuntimeError("Panel updater still has an active Job or Pod; do not apply workloads")
    if state["journal"] not in {"absent", "terminal"}:
        raise RuntimeError("Panel updater journal requires operator recovery; do not apply workloads")
'''

UPDATER_REMOTE = UPDATER_GUARD + r'''
import sys
try:
    if sys.argv[1] == "pause":
        cron = updater_kubectl(["get", "cronjob", "panel-auto-update", "--ignore-not-found", "-o", "json"])
        if cron is not None and cron.get("spec", {}).get("suspend") is not True:
            updater_kubectl(["patch", "cronjob", "panel-auto-update", "--type=merge",
                             "-p", '{"spec":{"suspend":true}}', "-o", "json"])
    elif sys.argv[1] != "status":
        raise RuntimeError("Invalid updater preflight operation")
    print(json.dumps(updater_snapshot()))
except (UpdaterUnavailable, KeyError, TypeError):
    print(json.dumps({"status": "unavailable"}))
'''

# Sent over stdin, never installed as a second deployment framework. Only these
# small status records return over SSH; Terraform output stays in the VM log.
APPLY_REMOTE = UPDATER_GUARD + r'''
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

mode, stage = sys.argv[1:3]
if stage not in ("platform", "workloads", "observability"):
    raise SystemExit("Invalid stage")
directory = Path("/opt/pz-infrastructure") / stage
source = directory / "reviewed.tfplan"
os.umask(0o077)

def fail(message):
    print(json.dumps({"error": message}))
    raise SystemExit(0)

if mode == "hash":
    if not source.is_file():
        fail("Saved reviewed.tfplan is missing")
    print(json.dumps({"sha256": hashlib.sha256(source.read_bytes()).hexdigest()}))
    raise SystemExit(0)

digest = sys.argv[3]
if not re.fullmatch(r"[0-9a-f]{64}", digest):
    fail("Invalid saved plan digest")
unit = "pz-terraform-" + stage + "-" + digest + ".service"
job = directory / ".apply" / digest
plan = job / "reviewed.tfplan"
log = job / "apply.log"

def properties():
    keys = ("LoadState", "ActiveState", "SubState", "Result", "ExecMainCode", "ExecMainStatus")
    result = subprocess.run(["systemctl", "show", unit, "--property=" + ",".join(keys)],
                            text=True, capture_output=True, timeout=10)
    values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    return {key: values[key] for key in keys if key in values}

if mode == "launch":
    job.mkdir(mode=0o700, parents=True, exist_ok=True)
    job.chmod(0o700)
    with (job / "control.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if properties().get("LoadState") == "not-found":
            marker = job / "launch-attempt"
            if marker.exists():
                fail("A launch was already attempted but its unit is missing; inspect on the VM, do not relaunch")
            if stage == "workloads":
                try:
                    require_updater_idle()
                except RuntimeError as error:
                    fail(str(error))
                except (UpdaterUnavailable, KeyError, TypeError):
                    fail("Could not confirm that the panel updater is idle; no apply was launched")
            if plan.exists():
                if hashlib.sha256(plan.read_bytes()).hexdigest() != digest:
                    fail("Immutable saved plan digest differs; inspect on the VM")
            else:
                data = source.read_bytes()
                if hashlib.sha256(data).hexdigest() != digest:
                    fail("reviewed.tfplan changed before launch; review the new plan first")
                with plan.open("xb") as output:
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
            plan.chmod(0o400)
            terraform = shutil.which("terraform")
            if not terraform:
                fail("Terraform executable is missing on the VM")
            for state in directory.glob("terraform.tfstate*"):
                if state.is_file():
                    state.chmod(0o600)
            with log.open("ab"):
                pass
            log.chmod(0o600)
            # Persistent at-most-once marker: an ambiguous launch or a reboot
            # requires inspection, never another automatic apply of this hash.
            with marker.open("xb") as output:
                output.write((unit + "\n").encode())
                output.flush()
                os.fsync(output.fileno())
            for parent in (job, job.parent, directory):
                descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            result = subprocess.run([
                "systemd-run", "--quiet", "--no-block", "--unit=" + unit,
                "--service-type=exec", "--remain-after-exit", "--property=Restart=no",
                "--property=UMask=0077", "--property=StandardOutput=append:" + str(log),
                "--property=StandardError=append:" + str(log),
                "--setenv=TF_IN_AUTOMATION=1",
                "--setenv=TF_CLI_CONFIG_FILE=/opt/pz-infrastructure/terraform.rc",
                terraform, "-chdir=" + str(directory), "apply", "-input=false", "-no-color", str(plan),
            ], capture_output=True, timeout=15)
            if result.returncode:
                with log.open("ab") as output:
                    output.write(result.stdout + result.stderr)
                fail("systemd-run rejected the launch; inspect the VM log, no automatic retry")
elif mode != "status":
    fail("Invalid apply operation")
print(json.dumps(properties()))
'''


def ssh(command, **kwargs):
    return subprocess.run([
        "ssh", "-F", "/dev/null", "-i", os.path.expanduser("~/.ssh/matveevmihail_yt"),
        "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=15",
        "matveevmihail@51.250.40.253", command,
    ], check=True, **kwargs)


def apply_request(mode, stage, digest, deadline):
    timeout = min(45, deadline - time.monotonic())
    if timeout <= 0:
        raise TimeoutError("Apply observation deadline reached")
    command = "sudo -n python3 - " + shlex.join([mode, stage] + ([digest] if digest else []))
    result = ssh(command, input=APPLY_REMOTE, text=True, capture_output=True, timeout=timeout)
    payload = json.loads(result.stdout)
    if "error" in payload:
        raise RuntimeError(payload["error"])
    return payload


def updater_request(mode, deadline):
    timeout = min(60, deadline - time.monotonic())
    if timeout <= 0:
        raise TimeoutError("Panel updater pause observation deadline reached")
    result = ssh("sudo -n python3 - " + shlex.quote(mode), input=UPDATER_REMOTE,
                 text=True, capture_output=True, timeout=timeout)
    return json.loads(result.stdout)


def pause_updater_for_plan():
    deadline = time.monotonic() + UPDATER_PAUSE_TIMEOUT
    transport_errors = (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError)
    print("Pausing the panel updater before workloads planning.", flush=True)
    try:
        updater_request("pause", deadline)
    except transport_errors:
        print("Pause response lost; reconnecting only to inspect updater status.", flush=True)
    previous = None
    while time.monotonic() < deadline:
        try:
            state = updater_request("status", deadline)
            if state.get("status") == "ok":
                if not state["suspended"]:
                    raise RuntimeError("Panel updater suspension is unconfirmed; no plan created. Re-run workloads plan.")
                if not state["active_jobs"] and not state["active_pods"]:
                    if state["journal"] not in {"absent", "terminal"}:
                        raise RuntimeError("Panel updater journal requires operator recovery; no workloads plan created")
                    print("Updater paused until reviewed workloads apply resumes configured schedule.", flush=True)
                    return
                summary = (state["active_jobs"], state["active_pods"])
                if summary != previous:
                    print(f"Waiting for updater: {summary[0]} active Job(s), {summary[1]} active Pod(s).", flush=True)
                    previous = summary
        except transport_errors:
            if previous != "ssh-unavailable":
                print("SSH unavailable; retrying updater status only.", flush=True)
                previous = "ssh-unavailable"
        time.sleep(min(5, max(0, deadline - time.monotonic())))
    raise TimeoutError("Panel updater did not become safely idle within 15 minutes; no workloads plan created. "
                       "Its running job was not stopped and its schedule remains paused if the pause succeeded.")


def apply(stage):
    deadline = time.monotonic() + APPLY_TIMEOUT
    transport_errors = (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError)
    # This read-only request may be retried; once launch is sent, reconnects
    # below only query systemd and cannot start another apply.
    while True:
        try:
            digest = apply_request("hash", stage, None, deadline)["sha256"]
            break
        except transport_errors:
            if time.monotonic() >= deadline:
                raise TimeoutError("Could not read the saved plan within 20 minutes; no launch sent")
            time.sleep(min(15, max(0, deadline - time.monotonic())))
    unit = f"pz-terraform-{stage}-{digest}.service"
    print(f"Apply unit: {unit}", flush=True)
    print(f"VM-only log: {REMOTE_ROOT}/{stage}/.apply/{digest}/apply.log", flush=True)
    try:
        apply_request("launch", stage, digest, deadline)
    except transport_errors:
        print("Launch response lost; reconnecting only to inspect this unit.", flush=True)
    previous = None
    while time.monotonic() < deadline:
        try:
            state = apply_request("status", stage, digest, deadline)
            summary = (state.get("LoadState"), state.get("ActiveState"), state.get("SubState"))
            if summary != previous:
                print("Unit status: " + "/".join(value or "unknown" for value in summary), flush=True)
                previous = summary
            if (summary == ("loaded", "active", "exited") and state.get("Result") == "success"
                    and state.get("ExecMainCode") == "1" and state.get("ExecMainStatus") == "0"):
                print("Terraform apply confirmed: systemd exit 0.", flush=True)
                return
            if state.get("ActiveState") == "failed" or state.get("Result") not in (None, "", "success"):
                raise RuntimeError(f"Apply unit failed: {unit}; inspect its VM-only log, no automatic restart")
        except transport_errors:
            if previous != "ssh-unavailable":
                print("SSH unavailable; the VM job is unchanged. Retrying status only.", flush=True)
                previous = "ssh-unavailable"
        time.sleep(min(15, max(0, deadline - time.monotonic())))
    raise TimeoutError(f"20-minute observation deadline reached for {unit}; result is unconfirmed, "
                       "the VM job was not stopped. Re-run apply with the same saved plan to inspect it.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("sync", "plan", "apply", "status"))
    parser.add_argument("stage", choices=STAGES, nargs="?")
    args = parser.parse_args()
    if args.action == "sync":
        # Explicit extensions; never include state, credentials, local variables,
        # saved plans, .terraform caches, or cloud/bootstrap adapter states.
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            for stage in STAGES:
                for path in sorted((ROOT / stage).iterdir()):
                    if path.is_file() and (path.suffix in (".tf", ".yaml") or path.name in (".terraform.lock.hcl", "Caddyfile")):
                        archive.add(path, arcname=f"{stage}/{path.name}", recursive=False)
            # Workloads ConfigMap reads this exact sibling path. Do not sync the
            # whole directory: updater state/backups and credentials stay private.
            updater = ROOT / "panel-updater" / "updater.py"
            if not updater.is_file() or updater.is_symlink():
                raise RuntimeError("Expected regular panel-updater/updater.py source is missing")
            archive.add(updater, arcname="panel-updater/updater.py", recursive=False)
            # Exact runtime modules, never adjacent tests, snapshots or data.
            for name in TELEMETRY_MODULES:
                path = ROOT / "panel-telemetry" / name
                if not path.is_file() or path.is_symlink():
                    raise RuntimeError("Expected regular panel telemetry module")
                archive.add(path, arcname=f"panel-telemetry/{path.name}", recursive=False)
        ssh(f"sudo -n install -d -m 700 {REMOTE_ROOT}")
        ssh(f"sudo -n tar -xzf - --no-same-owner -C {REMOTE_ROOT}", input=buffer.getvalue())
    elif args.action == "status":
        ssh("sudo -n k3s kubectl --kubeconfig /etc/rancher/k3s/operator.yaml get nodes,pods,pvc,resourcequota -A")
    else:
        if not args.stage:
            parser.error("plan/apply require a stage")
        directory = f"{REMOTE_ROOT}/{args.stage}"
        common = f"sudo -n env TF_IN_AUTOMATION=1 TF_CLI_CONFIG_FILE={REMOTE_ROOT}/terraform.rc terraform -chdir={shlex.quote(directory)}"
        if args.action == "plan":
            if args.stage == "workloads":
                pause_updater_for_plan()
            ssh(common + " init -input=false -lockfile=readonly -no-color")
            ssh(common + " plan -input=false -out=reviewed.tfplan -no-color")
            ssh(f"sudo -n chmod 600 {directory}/reviewed.tfplan")
        else:
            apply(args.stage)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, TimeoutError) as error:
        raise SystemExit(str(error)) from None
