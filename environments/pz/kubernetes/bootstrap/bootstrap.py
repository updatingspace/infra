#!/usr/bin/env python3
"""Pinned k3s bootstrap in a persistent VM job, observed over reconnectable SSH."""
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time

EXPECTED_HOST = "compute-vm-2-6-60-ssd-1785610759198"
JOB = Path("/var/lib/pz-bootstrap/job")
WAIT_SECONDS = 20 * 60
POLL_SECONDS = 15

# Executed only by the systemd-owned worker. Installer output stays in the
# root-only VM log, and admin credentials never cross the SSH connection.
REMOTE = r'''
import base64, hashlib, json, os, pathlib, subprocess, sys, time, urllib.request
def wait_node_ready(operator, expected, timeout=180):
    # The installer can return before the kubelet registers its Node. A direct
    # kubectl wait exits immediately on NotFound instead of waiting for creation.
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SystemExit('Kubernetes node did not become Ready within 180 seconds')
        try:
            response = subprocess.run([
                '/usr/local/bin/k3s', 'kubectl', '--kubeconfig', str(operator),
                '--request-timeout=10s', 'get', 'node', expected, '-o', 'json',
            ], text=True, capture_output=True, timeout=min(15, remaining))
            node = json.loads(response.stdout) if response.returncode == 0 else {}
            if isinstance(node, dict) and node.get('metadata', {}).get('name') == expected and any(
                condition.get('type') == 'Ready' and condition.get('status') == 'True'
                for condition in node.get('status', {}).get('conditions', [])
            ):
                return
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass  # Transient API startup failure; captured output stays private.
        time.sleep(min(3, max(0, deadline - time.monotonic())))
os.umask(0o077)
p=json.load(sys.stdin)
expected='compute-vm-2-6-60-ssd-1785610759198'
assert subprocess.check_output(['hostname'],text=True).strip()==expected, 'Unexpected host'
version=p['version']; config=base64.b64decode(p['config'])
root=pathlib.Path('/etc/rancher/k3s'); root.mkdir(parents=True,exist_ok=True)
configpath=root/'config.yaml'
installed=pathlib.Path('/usr/local/bin/k3s').exists()
if installed:
    current=subprocess.check_output(['/usr/local/bin/k3s','--version'],text=True).split()[2]
    if current!=version or not configpath.exists() or configpath.read_bytes()!=config:
        raise SystemExit('Existing k3s differs. Review a maintenance upgrade instead of replacing it automatically.')
else:
    backup=pathlib.Path('/var/backups/pz-k3s-20260930/pz-stack.tar.gz')
    assert backup.is_file() and backup.stat().st_size>0, 'Verified migration backup required'
    assert pathlib.Path('/var/backups/pz-k3s-20260930/verified.json').exists(), 'Backup verification missing'
    assert not configpath.exists(), 'Unmanaged config exists; inspect before bootstrap'
    url=f'https://raw.githubusercontent.com/k3s-io/k3s/{version}/install.sh'
    script=urllib.request.urlopen(url,timeout=60).read()
    assert hashlib.sha256(script).hexdigest()==p['installer_sha256'], 'Installer checksum mismatch'
    installer=pathlib.Path('/var/backups/pz-k3s-20260930/k3s-install.sh'); installer.write_bytes(script)
    configpath.write_bytes(config)
    env=dict(os.environ,INSTALL_K3S_VERSION=version,INSTALL_K3S_EXEC='server')
    subprocess.run(['sh',str(installer)],env=env,check=True)
# Keep admin credentials on the server under a stable operator filename.
# Current k3s uses the bind address; also normalize older loopback configs.
source=root/'k3s.yaml'
operator=root/'operator.yaml'
operator.write_text(source.read_text().replace('https://127.0.0.1:6443','https://10.130.0.30:6443'))
operator.chmod(0o600)
wait_node_ready(operator, expected)
print('Pinned k3s ready; credentials retained on server.')
'''

def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def identity(spec, script):
    digest = hashlib.sha256(canonical({"spec": spec, "script": script}).encode()).hexdigest()
    return {"digest": digest, "unit": "pz-k3s-bootstrap-" + digest + ".service"}


def private_file(path, content):
    require(not path.is_symlink(), "Bootstrap job files cannot be symlinks")
    if path.exists():
        require(path.is_file() and path.read_text() == content, "Immutable bootstrap job differs; inspect the VM before another attempt")
    else:
        with path.open("x", encoding="utf-8") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
    path.chmod(0o600)


def properties(unit):
    keys = ("LoadState", "ActiveState", "SubState", "Result", "ExecMainCode", "ExecMainStatus", "MainPID")
    response = subprocess.run(["systemctl", "show", unit, "--property=" + ",".join(keys)],
                              text=True, capture_output=True, timeout=10)
    result = dict(line.split("=", 1) for line in response.stdout.splitlines() if "=" in line)
    require(result.get("LoadState") in ("loaded", "not-found"), "Bootstrap unit status unavailable")
    return result


def node_ready():
    try:
        response = subprocess.run([
            "/usr/local/bin/k3s", "kubectl", "--kubeconfig", "/etc/rancher/k3s/operator.yaml",
            "--request-timeout=10s", "get", "node", EXPECTED_HOST, "-o", "json",
        ], text=True, capture_output=True, timeout=15)
        node = json.loads(response.stdout) if response.returncode == 0 else {}
        return node.get("metadata", {}).get("name") == EXPECTED_HOST and any(
            condition.get("type") == "Ready" and condition.get("status") == "True"
            for condition in node.get("status", {}).get("conditions", [])
        )
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False


def classify(state, current):
    require(state.get("LoadState") == "loaded", "Bootstrap unit missing; ambiguous launch or reboot requires VM inspection, no automatic relaunch")
    require(state.get("ActiveState") != "failed" and state.get("Result") in (None, "", "success"),
            "Bootstrap job failed; inspect the root-only VM log, no automatic restart")
    report = dict(current, state="running", node_ready=False)
    if state.get("ActiveState") == "active" and state.get("SubState") == "exited":
        require(state.get("ExecMainCode") == "1" and state.get("ExecMainStatus") == "0" and state.get("Result") == "success" and state.get("MainPID") == "0",
                "Bootstrap job lacks normal exit-zero proof")
        report["node_ready"] = node_ready()
        report["state"] = "succeeded" if report["node_ready"] else "waiting-for-node-ready"
    elif state.get("ActiveState") == "inactive":
        require(state.get("ExecMainCode") == "0", "Bootstrap job exited without retained success state; inspect the VM")
    return report


def manage(mode, payload):
    os.umask(0o077)
    require(os.geteuid() == 0, "Root privileges required for bootstrap job control")
    require(subprocess.check_output(["hostname"], text=True).strip() == EXPECTED_HOST, "Unexpected bootstrap host")
    spec, script = payload["spec"], payload["script"]
    current = identity(spec, script)
    require(mode in ("launch", "status"), "Invalid bootstrap operation")
    require(not JOB.is_symlink() and not JOB.parent.is_symlink(), "Bootstrap job directory cannot be a symlink")
    if mode == "launch":
        JOB.mkdir(mode=0o700, parents=True, exist_ok=True)
        JOB.chmod(0o700)
        JOB.parent.chmod(0o700)
    require(JOB.is_dir(), "Bootstrap job is absent; inspect the ambiguous launch before retrying")
    with (JOB / "control.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        metadata = JOB / "identity.json"
        marker = JOB / "launch-attempt"
        state = properties(current["unit"])
        if mode == "launch":
            require(metadata.exists() or state["LoadState"] == "not-found", "Unmanaged bootstrap unit already exists")
            private_file(metadata, canonical(current) + "\n")
            private_file(JOB / "bootstrap.py", script)
            private_file(JOB / "spec.json", canonical(spec) + "\n")
            if state["LoadState"] == "not-found":
                require(not marker.exists(), "Bootstrap launch was already attempted but its unit is missing; inspect the VM, no automatic relaunch")
                log = JOB / "bootstrap.log"
                require(not log.is_symlink(), "Bootstrap log cannot be a symlink")
                with log.open("ab"):
                    pass
                log.chmod(0o600)
                # Durable at-most-once marker precedes the launch request. If
                # this request is interrupted or the VM reboots, do not repeat it.
                private_file(marker, canonical(current) + "\n")
                for directory in (JOB, JOB.parent, JOB.parent.parent):
                    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                launched = subprocess.run([
                    "systemd-run", "--quiet", "--no-block", "--unit=" + current["unit"],
                    "--service-type=exec", "--remain-after-exit", "--property=Restart=no",
                    "--property=UMask=0077", "--property=StandardOutput=append:" + str(log),
                    "--property=StandardError=append:" + str(log),
                    "/usr/bin/python3", str(JOB / "bootstrap.py"), "--run-job",
                ], capture_output=True, timeout=15)
                require(launched.returncode == 0, "systemd-run rejected bootstrap; inspect the VM, no automatic retry")
                state = properties(current["unit"])
        require(metadata.is_file() and json.loads(metadata.read_text()) == current, "Bootstrap job identity differs")
        require(marker.is_file() and json.loads(marker.read_text()) == current, "Bootstrap launch marker missing or different")
        require((JOB / "bootstrap.py").read_text() == script and json.loads((JOB / "spec.json").read_text()) == spec,
                "Bootstrap worker or specification changed")
        return classify(state, current)


def run_job():
    os.umask(0o077)
    spec = json.loads((JOB / "spec.json").read_text())
    current = identity(spec, Path(__file__).read_text())
    require(json.loads((JOB / "identity.json").read_text()) == current and json.loads((JOB / "launch-attempt").read_text()) == current,
            "Bootstrap worker identity mismatch")
    # The transient unit owns this child process group, independently of SSH.
    subprocess.run(["/usr/bin/python3", "-c", REMOTE], input=canonical(spec), text=True, check=True)


class TransportError(RuntimeError):
    pass


def request(mode, spec, script, timeout):
    command = "sudo -n python3 -c " + shlex.quote(script) + " --manage-job " + mode
    try:
        response = subprocess.run([
            "ssh", "-F", "/dev/null", "-i", os.environ["SSH_KEY"],
            "-o", "BatchMode=yes", "-o", "ConnectionAttempts=1", "-o", "ConnectTimeout=15",
            "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=2", os.environ["SSH_TARGET"], command,
        ], input=canonical({"spec": spec, "script": script}), text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise TransportError("SSH request timed out") from None
    if response.returncode == 255:
        raise TransportError("SSH disconnected")
    require(response.returncode == 0, "Bootstrap controller failed; inspect the root-only VM log")
    try:
        result = json.loads(response.stdout)
    except ValueError:
        raise TransportError("Incomplete SSH status response") from None
    require(all(result.get(key) == value for key, value in identity(spec, script).items()), "Unexpected bootstrap status identity")
    require("error" not in result, result.get("error", "Bootstrap controller error"))
    if result.get("state") == "controller-timeout":
        raise TransportError("Bootstrap status inspection timed out")
    return result


def wait_job(spec, script, timeout=WAIT_SECONDS):
    deadline = time.monotonic() + timeout
    print("Bootstrap unit: " + identity(spec, script)["unit"], flush=True)
    print("VM-only log: " + str(JOB / "bootstrap.log"), flush=True)
    try:
        request("launch", spec, script, min(45, timeout))
    except TransportError:
        print("Launch response lost; reconnecting only to inspect the existing bootstrap job.", flush=True)
    previous = None
    while time.monotonic() < deadline:
        try:
            status = request("status", spec, script, min(45, max(0.1, deadline - time.monotonic())))
            require(status.get("state") in ("running", "waiting-for-node-ready", "succeeded"), "Invalid bootstrap job state")
            if status["state"] != previous:
                print("Bootstrap status: " + status["state"], flush=True)
                previous = status["state"]
            if status["state"] == "succeeded":
                require(status.get("node_ready") is True, "Bootstrap did not confirm NodeReady")
                print("Pinned k3s confirmed: systemd normal exit 0 and NodeReady; credentials retained on VM.", flush=True)
                return
        except TransportError:
            if previous != "ssh-unavailable":
                print("SSH unavailable; retrying bootstrap status only.", flush=True)
                previous = "ssh-unavailable"
        time.sleep(min(POLL_SECONDS, max(0, deadline - time.monotonic())))
    raise RuntimeError("20-minute bootstrap observation deadline reached; result unconfirmed. The VM job was not stopped; reconnect with the same code/spec to inspect it")


def main():
    if sys.argv[1:] == ["--run-job"]:
        run_job()
    elif len(sys.argv) == 3 and sys.argv[1] == "--manage-job":
        payload = json.load(sys.stdin)
        try:
            result = manage(sys.argv[2], payload)
        except subprocess.TimeoutExpired:
            result = dict(identity(payload["spec"], payload["script"]), state="controller-timeout")
        except RuntimeError as error:
            result = dict(identity(payload["spec"], payload["script"]), error=str(error))
        except Exception:
            result = dict(identity(payload["spec"], payload["script"]), error="Bootstrap controller could not verify job state; inspect the VM")
        print(canonical(result))
    else:
        require(not sys.argv[1:], "Unexpected bootstrap arguments")
        payload = {"version": os.environ["K3S_VERSION"], "installer_sha256": os.environ["INSTALLER_SHA256"], "config": os.environ["K3S_CONFIG_B64"]}
        base64.b64decode(payload["config"], validate=True)
        wait_job(payload, Path(__file__).read_text())


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as error:
        raise SystemExit(str(error)) from None
    except (OSError, ValueError, subprocess.CalledProcessError):
        # Do not expose subprocess argv: they may contain the private config.
        raise SystemExit("Bootstrap not confirmed; inspect /var/lib/pz-bootstrap/job on the VM. No automatic job restart or rollback was performed.") from None
