#!/usr/bin/env python3
"""Safe live admission/TCP checks; run as root on the VM AFTER workloads are Ready.

Creates no pods/jobs: the only admission request uses --dry-run=server. Network
probes only connect and close TCP sockets: no HTTP request or response is read.
stdout is safe JSON; exit 0 requires conclusive success for both kinds of checks.
Do not use this script to infer external game reachability or Monium delivery.
"""
import datetime
from decimal import Decimal
import ipaddress
import json
import os
import socket
import subprocess
import sys
import time

NODE = "compute-vm-2-6-60-ssd-1785610759198"
KUBECTL = ["/usr/local/bin/k3s", "kubectl", "--kubeconfig", "/etc/rancher/k3s/operator.yaml", "--request-timeout=15s"]
METADATA_IP = "169.254.169.254"
TIMEOUT = 3


def run(args, stdin=None):
    return subprocess.run(KUBECTL + args, input=stdin, text=True, capture_output=True, timeout=30, check=False)


def read(kind, name, namespace):
    result = run(["get", kind, name, "-n", namespace, "-o", "json"])
    if result.returncode:
        raise RuntimeError("Kubernetes read failed")
    return json.loads(result.stdout)


def cpu_m(value):
    value = str(value)
    suffixes = {"n": Decimal("0.000001"), "u": Decimal("0.001"), "m": Decimal(1)}
    if value[-1:] in suffixes:
        return Decimal(value[:-1]) * suffixes[value[-1]]
    return Decimal(value) * 1000


def connect(host, port):
    started = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=TIMEOUT):
            return {"connected": True, "elapsed_ms": round((time.monotonic() - started) * 1000)}
    except OSError as error:
        return {"connected": False, "error_type": type(error).__name__, "errno": error.errno,
                "elapsed_ms": round((time.monotonic() - started) * 1000)}


def pod_ready(pod):
    return (pod.get("status", {}).get("phase") == "Running"
            and not pod["metadata"].get("deletionTimestamp")
            and any(c.get("type") == "Ready" and c.get("status") == "True"
                    for c in pod.get("status", {}).get("conditions", [])))


def quota_probe(game):
    quota = read("resourcequota", "environment-budget", "zomboid")
    hard = cpu_m(quota["spec"]["hard"]["limits.cpu"])
    used = cpu_m(quota["status"]["used"]["limits.cpu"])
    # Exceed the aggregate limit by >=1m. At current 2600m usage this requests
    # only 1m, well below the per-container LimitRange ceiling of 2600m.
    probe_cpu = max(1, int(hard - used) + 1)
    name = "codex-quota-dryrun-" + str(time.time_ns())
    container = next(c for c in game["spec"]["containers"] if c["name"] == "zomboid")
    manifest = {
        "apiVersion": "v1", "kind": "Pod", "metadata": {"name": name, "namespace": "zomboid"},
        "spec": {
            "restartPolicy": "Never", "automountServiceAccountToken": False,
            "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "seccompProfile": {"type": "RuntimeDefault"}},
            "containers": [{
                "name": "admission-only", "image": container["image"], "imagePullPolicy": "Never",
                "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}},
                "resources": {
                    "requests": {"cpu": "1m", "memory": "1Mi", "ephemeral-storage": "1Mi"},
                    "limits": {"cpu": str(probe_cpu) + "m", "memory": "1Mi", "ephemeral-storage": "1Mi"},
                },
            }],
        },
    }
    response = run(["create", "--dry-run=server", "-f", "-", "-o", "json"], json.dumps(manifest))
    error = response.stderr.lower()
    forbidden = "forbidden" in error
    quota_denied = forbidden and "exceeded quota" in error and "limits.cpu" in error
    range_denied = forbidden and "maximum cpu usage per container" in error
    after = run(["get", "pod", name, "-n", "zomboid", "--ignore-not-found", "-o", "name"])
    absent = after.returncode == 0 and not after.stdout.strip()
    evidence = {
        "dry_run": "server", "quota_cpu_m": str(hard), "used_cpu_m": str(used),
        "probe_limit_cpu_m": probe_cpu, "aggregate_exceeds_quota": used + probe_cpu > hard,
        "pod_absence_verified": absent,
        "admission": "rejected" if response.returncode else "accepted",
        "matched_reason": "CPU ResourceQuota" if quota_denied else "CPU LimitRange" if range_denied else None,
    }
    passed = response.returncode != 0 and quota_denied and absent and used + probe_cpu > hard
    evidence["status"] = "passed" if passed else "failed" if response.returncode == 0 else "inconclusive"
    if evidence["status"] == "inconclusive":
        evidence["reason"] = "No recognized CPU ResourceQuota rejection with confirmed absence of the dry-run pod. A LimitRange rejection alone does not verify aggregate quota enforcement. Raw API errors are not printed."
    return evidence


# Executed inside the EXISTING game container. Only numeric destinations supplied
# by this script are used; no DNS, shell, HTTP payload, body or environment access.
POD_PROBE = r'''
import json, socket, sys, time
result = {}
for name, host, port in json.loads(sys.argv[1]):
    started = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=3):
            result[name] = {"connected": True, "elapsed_ms": round((time.monotonic()-started)*1000)}
    except OSError as error:
        result[name] = {"connected": False, "error_type": type(error).__name__, "errno": error.errno,
                        "elapsed_ms": round((time.monotonic()-started)*1000)}
print(json.dumps(result))
'''


def network_probe():
    service = read("service", "panel", "zomboid")
    address = service["spec"]["clusterIP"]
    if ipaddress.ip_address(address) not in ipaddress.ip_network("10.43.0.0/16"):
        raise ValueError("Unexpected panel ClusterIP")
    if not any(p.get("port") == 3001 and p.get("protocol", "TCP") == "TCP" for p in service["spec"]["ports"]):
        raise ValueError("Unexpected panel service port")
    host_control = connect(METADATA_IP, 80)
    targets = [["panel", address, 3001], ["metadata", METADATA_IP, 80]]
    result = run(["exec", "-n", "zomboid", "zomboid-0", "-c", "zomboid", "--", "python3", "-c", POD_PROBE, json.dumps(targets)])
    if result.returncode:
        raise RuntimeError("Existing game pod TCP probe failed")
    probes = json.loads(result.stdout)
    panel_allowed = probes["panel"]["connected"] is True
    metadata_denied = probes["metadata"]["connected"] is False
    evidence = {
        "panel_service": {"address": address, "port": 3001, **probes["panel"]},
        "metadata_from_host": host_control, "metadata_from_game_pod": probes["metadata"],
        "only_tcp_connect_no_http": True,
        "attribution_limit": "Reachability is observed; this test does not identify which individual network rule dropped or rejected a packet.",
    }
    if not panel_allowed or not metadata_denied:
        evidence["status"] = "failed"
        evidence["reason"] = "Panel TCP connect must succeed and metadata TCP connect from the game pod must fail."
    elif not host_control["connected"]:
        evidence["status"] = "inconclusive"
        evidence["reason"] = "Metadata endpoint was also unreachable from the host; pod failure alone cannot prove isolation."
    else:
        evidence["status"] = "passed"
        evidence["reason"] = "Panel is reachable from the game pod; the host can connect to metadata while the game pod cannot."
    return evidence


def main():
    report = {"checked_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(), "node": NODE}
    try:
        if os.geteuid() != 0 or socket.gethostname() != NODE:
            raise RuntimeError("Must run as root on the intended VM")
        game = read("pod", "zomboid-0", "zomboid")
        if game.get("spec", {}).get("nodeName") != NODE:
            raise RuntimeError("Game pod belongs to a different node")
        if not pod_ready(game):
            report["precondition"] = {"status": "inconclusive", "reason": "Game pod is not Ready; wait for world startup before these checks."}
        else:
            for name, function in (("quota", lambda: quota_probe(game)), ("network", network_probe)):
                try:
                    report[name] = function()
                except Exception as error:
                    report[name] = {"status": "inconclusive", "error_type": type(error).__name__, "reason": "The check could not complete. API responses, stderr, pod specifications and environment are not included."}
    except Exception as error:
        report["precondition"] = {"status": "inconclusive", "error_type": type(error).__name__, "reason": "Cannot verify the expected root VM and Ready game pod."}
    report["ok"] = all(report.get(name, {}).get("status") == "passed" for name in ("quota", "network"))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
