#!/usr/bin/env python3
"""Verify one panel pod replacement without restarting the Ready game.

Default: delete exactly one Ready panel pod normally, then observe its replacement.
--observe-only: take a baseline before a planned panel update and observe it;
this mode never deletes a pod and may wait up to 900s for an updater transaction.
Both modes default to 180s and require a new Ready panel plus unchanged game pod
UID/container ID/restart count/start time and RCON players. Delete mode is capped
at 180s.
Run on the VM. Output is selected JSON only; no raw command output or player names.
"""
import argparse
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import time

EXPECTED_HOST = "compute-vm-2-6-60-ssd-1785610759198"
RCON_PLAYERS = "import json,re,runpy; h=runpy.run_path('/usr/local/bin/pz-game'); reply=h['rcon']('players'); m=re.search(r'Players connected\\s*\\((\\d+)\\)',reply,re.I); print(json.dumps({'players_count':int(m.group(1)) if m else None}))"


class VerificationError(Exception):
    pass


def require(condition, message):
    if not condition:
        raise VerificationError(message)


def ready(pod):
    return not pod.get("metadata", {}).get("deletionTimestamp") and pod.get("status", {}).get("phase") == "Running" and any(
        condition.get("type") == "Ready" and condition.get("status") == "True"
        for condition in pod.get("status", {}).get("conditions", [])
    )


def owned_by(obj, kind, name, uid):
    return any(owner.get("kind") == kind and owner.get("name") == name and owner.get("uid") == uid and owner.get("controller") is True
               for owner in obj.get("metadata", {}).get("ownerReferences", []))


def game_identity(pod):
    require(pod.get("metadata", {}).get("name") == "zomboid-0" and ready(pod), "Game must remain Ready")
    require(pod.get("spec", {}).get("nodeName") == EXPECTED_HOST, "Game is on an unexpected node")
    containers = [item for item in pod.get("status", {}).get("containerStatuses", []) if item.get("name") == "zomboid"]
    require(len(containers) == 1 and containers[0].get("ready") is True, "Expected one Ready game container")
    container = containers[0]
    result = {
        "pod_uid": pod["metadata"].get("uid"), "container_id": container.get("containerID"),
        "restarts": container.get("restartCount"), "started_at": container.get("state", {}).get("running", {}).get("startedAt"),
    }
    require(all(isinstance(result[key], str) and result[key] for key in ("pod_uid", "container_id", "started_at"))
            and type(result["restarts"]) is int and result["restarts"] >= 0, "Game runtime identity is incomplete")
    return result


def panel_pods(pods, replicasets, deployment):
    owned_sets = {item["metadata"]["uid"]: item["metadata"]["name"] for item in replicasets
                  if owned_by(item, "Deployment", "panel", deployment["metadata"]["uid"])}
    result = []
    for pod in pods:
        is_owned = any(owned_by(pod, "ReplicaSet", name, uid) for uid, name in owned_sets.items())
        labeled = pod.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/name") == "panel"
        if labeled:
            require(is_owned, "A panel-labeled pod is not owned by the expected Deployment")
        if is_owned:
            result.append(pod)
    return result


def panel_identity(pod):
    statuses = [item for item in pod.get("status", {}).get("containerStatuses", []) if item.get("name") == "panel"]
    require(ready(pod) and len(statuses) == 1 and statuses[0].get("ready") is True, "Replacement panel container is not Ready")
    require(pod.get("spec", {}).get("nodeName") == EXPECTED_HOST, "Panel is on an unexpected node")
    return {"name": pod["metadata"]["name"], "uid": pod["metadata"]["uid"], "image_id": statuses[0].get("imageID")}


class Verifier:
    def __init__(self, args):
        self.args = args
        self.deadline = time.monotonic() + args.wait_seconds + 120
        self.panel_deadline = None
        self.kubectl = ["/usr/local/bin/k3s", "kubectl", "--kubeconfig", args.kubeconfig, "--request-timeout=10s", "-n", "zomboid"]
        self.report = {"ok": False, "mode": "observe-only" if args.observe_only else "restart-panel-once", "delete_attempted": False, "game_unchanged": False}

    def command(self, label, arguments, timeout=20):
        deadline = min(self.deadline, self.panel_deadline) if self.panel_deadline is not None else self.deadline
        remaining = deadline - time.monotonic()
        require(remaining > 0, "Verification deadline reached")
        try:
            response = subprocess.run(self.kubectl + arguments, text=True, capture_output=True, timeout=min(timeout, remaining))
        except (OSError, subprocess.TimeoutExpired):
            raise VerificationError(label + " failed or timed out; raw output suppressed") from None
        require(response.returncode == 0, label + " failed; raw output suppressed")
        return response.stdout

    def get(self, resource, name=None):
        arguments = ["get", resource] + ([name] if name else []) + ["-o", "json"]
        try:
            return json.loads(self.command("Kubernetes read", arguments))
        except ValueError:
            raise VerificationError("Kubernetes read returned invalid JSON") from None

    def game(self):
        pod = self.get("pod", "zomboid-0")
        require(owned_by(pod, "StatefulSet", "zomboid", self.game_controller_uid), "Game StatefulSet ownership changed")
        return game_identity(pod)

    def panel(self, allow_scaled_down=False):
        deployment = self.get("deployment", "panel")
        observing_update = allow_scaled_down and self.args.observe_only and hasattr(self, "deployment_uid")
        allowed_replicas = (0, 1) if observing_update else (1,)
        require(deployment.get("spec", {}).get("replicas") in allowed_replicas
                and deployment.get("spec", {}).get("strategy", {}).get("type") == "Recreate",
                "Panel Deployment must use Recreate and have one replica (zero only during update observation)")
        if hasattr(self, "deployment_uid"):
            require(deployment["metadata"]["uid"] == self.deployment_uid, "Panel Deployment was replaced")
            if not self.args.observe_only:
                require(deployment["metadata"].get("generation") == self.deployment_generation, "Panel template changed during the restart test")
        pods = panel_pods(self.get("pods").get("items", []), self.get("replicasets").get("items", []), deployment)
        return deployment, pods

    def players(self):
        raw = self.command("RCON players", ["exec", "zomboid-0", "-c", "zomboid", "--", "python3", "-c", RCON_PLAYERS], timeout=45)
        try:
            count = json.loads(raw).get("players_count")
        except ValueError:
            raise VerificationError("RCON players returned invalid safe JSON") from None
        require(type(count) is int and count >= 0, "RCON players response did not contain a valid count")
        return {"ok": True, "players_count": count}

    def verify(self):
        require(os.geteuid() == 0 and socket.gethostname() == EXPECTED_HOST, "Run as root on the expected game VM")
        require(Path("/usr/local/bin/k3s").is_file(), "k3s is missing")
        game_controller = self.get("statefulset", "zomboid")
        require(game_controller.get("spec", {}).get("replicas") == 1, "Game must have one desired replica before the test")
        self.game_controller_uid = game_controller["metadata"]["uid"]
        before = self.game()
        self.report["game_before"] = before
        self.report["rcon_before"] = self.players()
        deployment, pods = self.panel()
        require(len(pods) == 1 and ready(pods[0]), "Expected exactly one Ready panel pod and no old owned pods")
        old = panel_identity(pods[0])
        self.deployment_uid = deployment["metadata"]["uid"]
        self.deployment_generation = deployment["metadata"].get("generation")
        self.report["panel_before"] = old
        require(self.game() == before, "Game changed before the panel operation")
        self.panel_deadline = time.monotonic() + self.args.wait_seconds
        if not self.args.observe_only:
            self.report["delete_attempted"] = True
            # Default pod grace applies. Never force, patch, scale or repeat this
            # request; an ambiguous response requires operator inspection.
            self.command("Normal panel pod deletion", ["delete", "pod", old["name"], "--wait=false"])
        print(json.dumps({"event": "baseline-established", "mode": self.report["mode"], "panel_uid": old["uid"], "game": before}), flush=True)
        while True:
            require(time.monotonic() < self.panel_deadline,
                    f"New panel did not become Ready within its wait budget ({self.args.wait_seconds}s)")
            require(self.game() == before, "Game identity or restart count changed while panel was replaced")
            deployment, pods = self.panel(allow_scaled_down=True)
            if (deployment.get("spec", {}).get("replicas") == 1 and len(pods) == 1
                    and pods[0]["metadata"].get("uid") != old["uid"] and ready(pods[0])):
                new = panel_identity(pods[0])
                require(time.monotonic() < self.panel_deadline, "Panel replacement exceeded its wait budget")
                break
            time.sleep(min(3, max(0, self.panel_deadline - time.monotonic())))
        self.panel_deadline = None
        self.report["panel_after"] = new
        self.report["rcon_after"] = self.players()
        _, final_panels = self.panel()
        require(len(final_panels) == 1 and ready(final_panels[0]) and panel_identity(final_panels[0]) == new,
                "Panel changed again during final verification")
        after = self.game()
        require(after == before, "Game changed during final verification")
        self.report.update(ok=True, game_unchanged=True, game_after=after)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observe-only", action="store_true", help="Observe a planned update; send no delete request")
    parser.add_argument("--wait-seconds", type=int, default=180,
                        help="Replacement wait budget: 1..180 seconds, or 1..900 with --observe-only (default: 180)")
    parser.add_argument("--kubeconfig", default="/etc/rancher/k3s/operator.yaml")
    args = parser.parse_args()
    maximum = 900 if args.observe_only else 180
    if not 1 <= args.wait_seconds <= maximum:
        parser.error(f"--wait-seconds must be between 1 and {maximum} for this mode")
    return args


def main():
    args = parse_args()
    verifier = Verifier(args)
    def expired(_signum, _frame):
        raise VerificationError("Overall verification deadline reached")
    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, args.wait_seconds + 120)
    try:
        verifier.verify()
    except VerificationError as error:
        verifier.report["error"] = str(error)
    except (OSError, KeyError, IndexError, TypeError, ValueError):
        verifier.report["error"] = "Unexpected verifier data or local failure; private details suppressed"
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
    print(json.dumps(verifier.report, sort_keys=True), flush=True)
    return 0 if verifier.report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
