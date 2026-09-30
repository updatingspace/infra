#!/usr/bin/env python3
"""Read-only post-migration checks, run on the VM after Kubernetes startup.

Example (no credentials are passed on the command line):
  sudo python3 verify-game.py --https-domain panel.example.com

Uses Kubernetes reads and exec of read-only health probes, HTTP GETs and RCON
`players`. No saves, quits, restarts, writes, environment dumps, Secret reads
or application log reads.
Output is a selected JSON report; subprocess output and response bodies are
never printed. HTTPS is optional and validates the public certificate through
the node's Caddy listener without relying on external DNS/CDN routing.
"""
import argparse
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import time


EXPECTED_NODE = "compute-vm-2-6-60-ssd-1785610759198"
EXPECTED_MOUNTS = {
    "zomboid": {"pz-server": ("/pz-server", False), "zomboid": ("/zomboid", False), "steam": ("/home/steam/Steam", False)},
    "panel": {"panel": ("/app/data", False), "panel-logs": ("/app/logs", False), "pz-server": ("/pz-server", True), "zomboid": ("/zomboid", False)},
    "caddy": {"caddy-data": ("/data", False), "caddy-config": ("/config", False)},
}
NAMESPACES = {"zomboid": "zomboid", "panel": "zomboid", "caddy": "edge"}


class VerificationError(Exception):
    pass


def require(condition, message):
    if not condition:
        raise VerificationError(message)


def image_digest(value):
    match = re.search(r"sha256:[0-9a-f]{64}$", value or "")
    require(match is not None, "Runtime did not report an immutable image digest")
    return match.group(0)


def ready(pod):
    return (
        not pod.get("metadata", {}).get("deletionTimestamp")
        and pod.get("status", {}).get("phase") == "Running"
        and any(item.get("type") == "Ready" and item.get("status") == "True" for item in pod.get("status", {}).get("conditions", []))
    )


def container_of(pod, name):
    containers = [container for container in pod["spec"]["containers"] if container["name"] == name]
    require(len(containers) == 1, "Expected exactly one application container: " + name)
    return containers[0]


def pod_mounts(pod, app):
    container = container_of(pod, app)
    mounts = {item["name"]: item for item in container.get("volumeMounts", [])}
    volumes = {item["name"]: item for item in pod["spec"].get("volumes", [])}
    result = []
    for claim, (path, readonly) in EXPECTED_MOUNTS[app].items():
        require(claim in mounts and claim in volumes, app + " is missing expected volume " + claim)
        mount = mounts[claim]
        volume = volumes[claim]
        require(mount["mountPath"] == path and bool(mount.get("readOnly", False)) == readonly, app + " has unexpected mount settings for " + claim)
        require(volume.get("persistentVolumeClaim", {}).get("claimName") == claim, app + " is not using the expected PVC " + claim)
        require(not mount.get("subPath") and not mount.get("subPathExpr"), "Unexpected PVC subpath: " + claim)
        result.append({"claim": claim, "mount": path, "read_only": readonly})
    require(not any("hostPath" in volume for volume in volumes.values()), app + " has an unexpected hostPath volume")
    return result


class Verifier:
    def __init__(self, args):
        self.args = args
        self.deadline = time.monotonic() + args.wait_seconds + 120
        if Path("/usr/local/bin/k3s").exists():
            self.kubectl = ["/usr/local/bin/k3s", "kubectl"]
        else:
            executable = shutil.which("kubectl")
            require(executable is not None, "kubectl or k3s must be installed")
            self.kubectl = [executable]
        self.kubectl += ["--kubeconfig", args.kubeconfig, "--request-timeout=15s"]
        self.report = {"ok": False, "read_only": True, "node": EXPECTED_NODE, "pods": {}, "persistent_volumes": {}, "health": {}}

    def command(self, label, args, timeout=25):
        remaining = self.deadline - time.monotonic()
        require(remaining > 0, "Overall verification deadline reached")
        try:
            result = subprocess.run(self.kubectl + args, capture_output=True, text=True, timeout=min(timeout, remaining))
        except subprocess.TimeoutExpired:
            raise VerificationError(label + " timed out") from None
        require(result.returncode == 0, label + " failed (exit " + str(result.returncode) + "); subprocess output was suppressed")
        return result.stdout

    def get(self, label, args):
        value = self.command(label, ["get", *args, "-o", "json"])
        try:
            return json.loads(value)
        except ValueError:
            raise VerificationError(label + " returned invalid JSON") from None

    def exec(self, app, pod, label, argv, timeout=30):
        return self.command(label, ["-n", NAMESPACES[app], "exec", pod["metadata"]["name"], "-c", app, "--", *argv], timeout=timeout)

    def wait_ready(self):
        readiness_deadline = time.monotonic() + self.args.wait_seconds
        while True:
            pods = {}
            for app, namespace in NAMESPACES.items():
                listing = self.get(app + " pod status", ["pods", "-n", namespace, "-l", "app.kubernetes.io/name=" + app])
                active = [pod for pod in listing["items"] if not pod["metadata"].get("deletionTimestamp") and pod.get("status", {}).get("phase") not in {"Succeeded", "Failed"}]
                if len(active) == 1 and ready(active[0]):
                    pods[app] = active[0]
            if len(pods) == 3:
                return pods
            require(time.monotonic() < readiness_deadline, "Applications not Ready before timeout: " + ", ".join(sorted(set(NAMESPACES) - set(pods))))
            time.sleep(min(5, max(0, readiness_deadline - time.monotonic())))

    def check_controllers(self, pods):
        game = self.get("game StatefulSet", ["statefulset", "zomboid", "-n", "zomboid"])
        require(game["spec"].get("replicas") == 1 and game.get("status", {}).get("readyReplicas") == 1, "Game StatefulSet must have exactly one Ready replica")
        require(game["spec"].get("updateStrategy", {}).get("type") == "OnDelete", "Game updates must remain operator-controlled")
        pod = pods["zomboid"]
        require(pod["metadata"]["name"] == "zomboid-0", "Unexpected game pod identity")
        require(any(owner.get("kind") == "StatefulSet" and owner.get("uid") == game["metadata"]["uid"] for owner in pod["metadata"].get("ownerReferences", [])), "Game pod is not owned by the expected StatefulSet")
        require(pod["spec"].get("terminationGracePeriodSeconds", 0) >= 300, "Game shutdown grace is below 300 seconds")
        for app in ("panel", "caddy"):
            controller = self.get(app + " Deployment", ["deployment", app, "-n", NAMESPACES[app]])
            require(controller["spec"].get("replicas") == 1 and controller.get("status", {}).get("readyReplicas") == 1, app + " Deployment must have one Ready replica")
            require(controller["spec"].get("strategy", {}).get("type") == "Recreate", app + " Deployment must avoid concurrent writers")

    def check_pods(self, pods):
        for app, pod in pods.items():
            require(pod["spec"].get("nodeName") == EXPECTED_NODE, app + " is on an unexpected node")
            container = container_of(pod, app)
            if app == "panel":
                reference = container.get("image", "")
                official = re.fullmatch(r"ghcr\.io/fpsacha/zomboid-panel:v?\d+\.\d+\.\d+@sha256:[0-9a-f]{64}", reference)
                legacy = reference == "docker.io/local/pz-panel:migration-6a953f186a357932"
                require(bool(official) and container.get("imagePullPolicy") == "IfNotPresent"
                        or legacy and container.get("imagePullPolicy") == "Never",
                        "Panel must use a stable official digest or the exact migration image")
            else:
                require(container.get("imagePullPolicy") == "Never", app + " must use an imported local image")
            if app == "zomboid":
                resources = container.get("resources", {})
                # Kubernetes canonicalizes these quantities to the units below.
                require(resources.get("requests", {}).get("ephemeral-storage") == "1536Mi"
                        and resources.get("limits", {}).get("ephemeral-storage") == "3Gi",
                        "Running game pod must request 1536Mi and limit 3Gi ephemeral storage")
                self.report["game_ephemeral_storage"] = {"request": "1536Mi", "limit": "3Gi"}
            states = [state for state in pod.get("status", {}).get("containerStatuses", []) if state["name"] == app]
            require(len(states) == 1 and states[0].get("ready") is True, app + " container is not Ready")
            state = states[0]
            digest = image_digest(state.get("imageID"))
            expected = getattr(self.args, "expected_" + ("game" if app == "zomboid" else app) + "_image_id")
            if expected:
                require(digest == image_digest(expected), app + " runtime image digest differs from the expected imported image")
            self.report["pods"][app] = {
                "name": pod["metadata"]["name"], "uid": pod["metadata"]["uid"],
                "ready": True, "restarts": state.get("restartCount", 0), "image_id": digest,
                "mounts": pod_mounts(pod, app),
            }

    def check_volumes(self):
        checked = set()
        for app, mappings in EXPECTED_MOUNTS.items():
            namespace = NAMESPACES[app]
            for name in mappings:
                if name in checked:
                    continue
                checked.add(name)
                claim = self.get("PVC " + name, ["pvc", name, "-n", namespace])
                require(claim.get("status", {}).get("phase") == "Bound", "PVC is not Bound: " + name)
                volume = self.get("PV " + name, ["pv", claim["spec"]["volumeName"]])
                require(volume["spec"].get("persistentVolumeReclaimPolicy") == "Retain", "PV must retain data: " + name)
                require(volume["spec"].get("claimRef", {}).get("namespace") == namespace and volume["spec"]["claimRef"].get("name") == name, "PV has an unexpected claim binding: " + name)
                legacy = Path("/opt/pz-stack/data") / name
                expected_mount = Path("/srv/pz-storage") / namespace
                require(volume["spec"].get("local", {}).get("path") == str(legacy), "PV has an unexpected local path: " + name)
                require(legacy.is_symlink() and legacy.resolve() == expected_mount / name, "Legacy data path does not point to bounded storage: " + name)
                require(os.path.ismount(expected_mount) and legacy.is_dir(), "Bounded filesystem is not mounted: " + namespace)
                self.report["persistent_volumes"][name] = {"bound": True, "retained": True, "filesystem": str(expected_mount)}

    def check_health(self, pods):
        self.exec("zomboid", pods["zomboid"], "game health", ["python3", "/usr/local/bin/pz-game", "health"], timeout=45)
        self.report["health"]["game"] = {"ok": True}
        # This calls the existing helper directly, suppressing the player names.
        code = "import json,re,runpy; h=runpy.run_path('/usr/local/bin/pz-game'); reply=h['rcon']('players'); m=re.search(r'Players connected\\s*\\((\\d+)\\)',reply,re.I); print(json.dumps({'ok':True,'players_count':int(m.group(1)) if m else None}))"
        raw = self.exec("zomboid", pods["zomboid"], "RCON players", ["python3", "-c", code], timeout=45)
        try:
            players = json.loads(raw)
        except ValueError:
            raise VerificationError("RCON players wrapper returned invalid JSON") from None
        require(players.get("ok") is True, "RCON players did not succeed")
        count = players.get("players_count")
        require(type(count) is int and count >= 0, "RCON players response did not contain a valid player count")
        self.report["health"]["rcon_players"] = {"ok": True, "players_count": count}
        # Consume/discard HTTP response bodies; print only a numeric status.
        node_code = "const http=require('http');const r=http.get(process.argv[1],s=>{s.resume();s.on('end',()=>{console.log(JSON.stringify({status:s.statusCode}));process.exit(s.statusCode===200?0:2)})});r.setTimeout(10000,()=>{r.destroy();process.exit(3)});r.on('error',()=>process.exit(4));setTimeout(()=>process.exit(5),15000).unref();"
        for label, url in (
            ("panel_loopback", "http://127.0.0.1:3001/api/health"),
            ("panel_service", "http://panel.zomboid.svc.cluster.local:3001/api/health"),
            ("game_metrics_service", "http://zomboid.zomboid.svc.cluster.local:9090/metrics"),
        ):
            raw = self.exec("panel", pods["panel"], label, ["node", "-e", node_code, url])
            try:
                status = json.loads(raw).get("status")
            except ValueError:
                raise VerificationError(label + " returned invalid health JSON") from None
            require(status == 200, label + " did not return HTTP 200")
            self.report["health"][label] = {"status": 200}

    def check_https(self):
        if not self.args.https_domain:
            self.report["health"]["https"] = {"skipped": "Supply --https-domain to verify Caddy TLS and the edge-to-panel route"}
            return
        node = self.get("node address", ["node", EXPECTED_NODE])
        addresses = [address["address"] for address in node.get("status", {}).get("addresses", []) if address["type"] == "InternalIP"]
        require(addresses or self.args.https_address, "Node has no InternalIP for direct HTTPS verification")
        address = self.args.https_address or addresses[0]
        try:
            context = ssl.create_default_context()
            with socket.create_connection((address, 443), timeout=10) as tcp:
                with context.wrap_socket(tcp, server_hostname=self.args.https_domain) as connection:
                    connection.settimeout(15)
                    request = "GET /api/health HTTP/1.1\r\nHost: " + self.args.https_domain + "\r\nConnection: close\r\n\r\n"
                    connection.sendall(request.encode("ascii"))
                    response = http.client.HTTPResponse(connection)
                    response.begin()
                    require(response.status == 200, "Caddy HTTPS health did not return HTTP 200")
            self.report["health"]["https"] = {"status": 200, "certificate_valid": True, "route": "node Caddy -> panel service"}
        except (OSError, ssl.SSLError, http.client.HTTPException):
            raise VerificationError("Direct Caddy HTTPS/TLS verification failed; response details suppressed") from None

    def verify(self):
        require(socket.gethostname() == EXPECTED_NODE, "Run this verifier on the expected game VM")
        pods = self.wait_ready()
        self.check_controllers(pods)
        self.check_pods(pods)
        self.check_volumes()
        self.check_health(pods)
        self.check_https()
        # Read back identity/restarts to detect any restart during verification.
        for app, before in pods.items():
            after = self.get(app + " final pod status", ["pod", before["metadata"]["name"], "-n", NAMESPACES[app]])
            require(after["metadata"]["uid"] == before["metadata"]["uid"] and ready(after), app + " was replaced or lost readiness during verification")
            states = {state["name"]: state for state in after["status"]["containerStatuses"]}
            require(states[app].get("restartCount", 0) == self.report["pods"][app]["restarts"], app + " restarted during verification")
        self.report["ok"] = True


def arguments():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--kubeconfig", default="/etc/rancher/k3s/operator.yaml")
    parser.add_argument("--wait-seconds", type=int, default=1800, help="Ready wait budget, 0..1800 seconds (default 1800 / 30 minutes); other checks add at most 120 seconds")
    parser.add_argument("--https-domain", help="Public panel DNS name for TLS certificate validation; no scheme or path")
    parser.add_argument("--https-address", help="Optional node IP for HTTPS; defaults to the Kubernetes node InternalIP")
    for app in ("game", "panel", "caddy"):
        parser.add_argument("--expected-" + app + "-image-id", help="Optional expected sha256 digest of the imported runtime image")
    args = parser.parse_args()
    if not 0 <= args.wait_seconds <= 1800:
        parser.error("--wait-seconds must be between 0 and 1800")
    if args.https_domain and not re.fullmatch(r"(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", args.https_domain):
        parser.error("--https-domain must be a DNS name, without scheme, port or path")
    return args


def main():
    verifier = None
    try:
        verifier = Verifier(arguments())
        def expired(_signum, _frame):
            raise VerificationError("Overall verification deadline reached")
        signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, verifier.args.wait_seconds + 120)
        try:
            verifier.verify()
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
        print(json.dumps(verifier.report, indent=2, sort_keys=True))
        return 0
    except VerificationError as error:
        report = verifier.report if verifier else {"ok": False, "read_only": True}
        report["error"] = str(error)
    except (KeyError, IndexError, TypeError, ValueError, OSError) as error:
        report = verifier.report if verifier else {"ok": False, "read_only": True}
        report["error"] = "Unexpected verification failure: " + type(error).__name__ + "; details suppressed"
    print(json.dumps(report, indent=2, sort_keys=True))
    return 1


if __name__ == "__main__":
    sys.exit(main())
