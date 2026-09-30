#!/usr/bin/env python3
"""Observe existing zomboid-0 startup; never start/restart or modify an app.

Run as root on the VM. Samples append to a 0600 JSONL file every 15 seconds
for at most 20 minutes, ending after three consecutive Ready observations.
Only pod status and /tmp/parallelscatter* file metadata are read. No file
contents, environment values, player names, or raw command errors are logged.
The maximum is sampled: a brief peak between observations can be missed.
Exit 0 requires temporary files observed before three empty Ready samples
in the same container, with observed sizes below 3 GiB. Exit 2 is incomplete or
failed evidence, including observing only empty files from the first sample.
"""

import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import time


NODE = "compute-vm-2-6-60-ssd-1785610759198"
OUTPUT = "/var/backups/pz-k3s-20260930/startup-ephemeral-samples.jsonl"
INTERVAL = 15
LIMIT_BYTES = 3 * 1024 ** 3
KUBECTL = ["/usr/local/bin/k3s", "kubectl", "--kubeconfig",
           "/etc/rancher/k3s/operator.yaml", "--request-timeout=5s"]
POD_FIELDS = (
    ".metadata.uid", ".status.phase",
    '.status.conditions[?(@.type=="Ready")].status',
    ".metadata.deletionTimestamp",
    '.status.containerStatuses[?(@.name=="zomboid")].state.running.startedAt',
    '.status.containerStatuses[?(@.name=="zomboid")].restartCount',
)
POD_FORMAT = "jsonpath=" + "".join("{" + field + '} {"\\n"}' for field in POD_FIELDS)
TEMP_PROBE = r'''
import glob,json,os,stat
result={"matched_entries":0,"regular_files":0,"apparent_bytes":0,
        "allocated_bytes":0,"vanished_entries":0,"non_regular_entries":0,"stat_errors":0}
for path in glob.glob("/tmp/parallelscatter*"):
    result["matched_entries"]+=1
    try:
        item=os.lstat(path)
    except FileNotFoundError:
        result["vanished_entries"]+=1
        continue
    except OSError:
        result["stat_errors"]+=1
        continue
    if not stat.S_ISREG(item.st_mode):
        result["non_regular_entries"]+=1
        continue
    result["regular_files"]+=1
    result["apparent_bytes"]+=item.st_size
    result["allocated_bytes"]+=item.st_blocks*512
print(json.dumps(result))
'''
TEMP_FIELDS = (
    "matched_entries", "regular_files", "apparent_bytes", "allocated_bytes",
    "vanished_entries", "non_regular_entries", "stat_errors",
)


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def command(arguments, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None, "deadline"
    try:
        result = subprocess.run(KUBECTL + arguments, text=True, capture_output=True,
                                timeout=min(6, remaining), check=False)
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except OSError:
        return None, "unavailable"
    if result.returncode:
        return None, "command_failed"
    return result.stdout, None


def pod_status(deadline):
    output, error = command(["get", "pod", "zomboid-0", "-n", "zomboid",
                             "--ignore-not-found", "-o", POD_FORMAT], deadline)
    if error:
        return {"present": None, "error": error}
    if not output.strip():
        return {"present": False}
    fields = [line.strip() for line in output.splitlines()]
    if len(fields) != len(POD_FIELDS) or not fields[0]:
        return {"present": None, "error": "invalid_status_response"}
    uid, phase, ready, deleting, started, restarts = fields
    try:
        restarts = int(restarts) if restarts else None
    except ValueError:
        return {"present": None, "error": "invalid_restart_count"}
    return {"present": True, "uid": uid, "phase": phase,
            "ready": ready == "True" and phase == "Running" and not deleting,
            "terminating": bool(deleting), "container_started_at": started or None,
            "restart_count": restarts}


def generation(pod):
    return (pod.get("uid"), pod.get("container_started_at"), pod.get("restart_count"))


def sample(deadline):
    before = pod_status(deadline)
    result = {"type": "sample", "at_utc": utc_now(), "pod": before,
              "observation_consistent": False, "temp": None}
    if not before.get("present") or not before.get("container_started_at") or before.get("terminating"):
        return result
    output, error = command(["exec", "-n", "zomboid", "zomboid-0", "-c", "zomboid",
                             "--", "python3", "-c", TEMP_PROBE], deadline)
    if error:
        result["probe_error"] = error
        return result
    try:
        raw = json.loads(output)
        temp = {field: raw[field] for field in TEMP_FIELDS}
        if any(type(value) is not int or value < 0 for value in temp.values()):
            raise ValueError
    except (ValueError, KeyError, TypeError):
        result["probe_error"] = "invalid_probe_response"
        return result
    after = pod_status(deadline)
    result["pod_after_probe"] = after
    if not after.get("present") or generation(before) != generation(after):
        result["probe_error"] = "pod_or_container_changed_during_probe"
        return result
    result["temp"] = temp
    result["observation_consistent"] = not after.get("terminating")
    result["ready_during_probe"] = before.get("ready", False) and after.get("ready", False)
    return result


class Observations:
    def __init__(self):
        self.samples = 0
        self.valid_samples = 0
        self.max_bytes = None
        self.max_allocated = None
        self.max_files = None
        self.current_generation = None
        self.positive_generations = set()
        self.ready_samples = []

    def add(self, value):
        self.samples += 1
        pod = value.get("pod", {})
        current = generation(pod) if pod.get("present") else None
        if current != self.current_generation:
            self.current_generation = current
            self.ready_samples = []
        temp = value.get("temp")
        valid = (value.get("observation_consistent") and temp is not None
                 and temp["stat_errors"] == 0 and temp["non_regular_entries"] == 0)
        if not valid:
            self.ready_samples = []
            return
        self.valid_samples += 1
        self.max_bytes = max(self.max_bytes or 0, temp["apparent_bytes"])
        self.max_allocated = max(self.max_allocated or 0, temp["allocated_bytes"])
        self.max_files = max(self.max_files or 0, temp["regular_files"])
        if temp["regular_files"] > 0:
            self.positive_generations.add(current)
        if value.get("ready_during_probe"):
            # A disappearing file is not an observation of an empty glob.
            self.ready_samples.append(temp["matched_entries"] == 0)
        else:
            self.ready_samples = []

    def summary(self):
        ready = len(self.ready_samples) >= 3
        empty = ready and all(self.ready_samples[-3:])
        return {
            "type": "summary", "at_utc": utc_now(),
            "end_reason": "three_ready_observations" if ready else "observation_deadline",
            "sample_count": self.samples, "valid_temp_samples": self.valid_samples,
            "final_pod_uid": self.current_generation[0] if self.current_generation else None,
            "consecutive_ready_observations": len(self.ready_samples),
            "max_temp_bytes": self.max_bytes, "max_temp_allocated_bytes": self.max_allocated,
            "max_temp_file_count": self.max_files, "comparison_limit_bytes": LIMIT_BYTES,
            "all_observed_temp_sizes_below_3gib": (self.max_bytes is not None
                                                  and self.max_bytes < LIMIT_BYTES
                                                  and self.max_allocated < LIMIT_BYTES),
            "empty_at_three_ready_observations": empty,
            "cleanup_observed_in_same_container": empty and self.current_generation in self.positive_generations,
            "true_peak_verified": False,
            "limits": [
                "15-second metadata samples can miss a brief peak; maxima are observed values only.",
                "Only visible regular files matching /tmp/parallelscatter* are measured; other writable-layer data and open unlinked files are excluded.",
                "Empty Ready samples alone do not prove cleanup unless temporary files were previously observed in the same pod/container generation.",
                "No save-file contents, environment values, or raw logs were read by this monitor.",
            ],
        }


def observe(output, duration):
    start = time.monotonic()
    deadline = start + duration
    observations = Observations()

    def emit(record):
        output.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        output.flush()
        os.fsync(output.fileno())

    emit({"type": "run_started", "at_utc": utc_now(), "interval_seconds": INTERVAL,
          "maximum_duration_seconds": duration, "application_read_only": True,
          "glob": "/tmp/parallelscatter*", "expected_node": NODE})
    while time.monotonic() < deadline:
        sample_started = time.monotonic()
        value = sample(deadline)
        value["elapsed_seconds"] = round(time.monotonic() - start, 3)
        observations.add(value)
        emit(value)
        if len(observations.ready_samples) >= 3:
            break
        time.sleep(max(0, min(sample_started + INTERVAL, deadline) - time.monotonic()))
    summary = observations.summary()
    summary["elapsed_seconds"] = round(time.monotonic() - start, 3)
    emit(summary)
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if (summary["cleanup_observed_in_same_container"]
                 and summary["all_observed_temp_sizes_below_3gib"]) else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--duration-seconds", type=int, default=1200)
    parser.add_argument("--output", default=OUTPUT)
    options = parser.parse_args()
    if not 1 <= options.duration_seconds <= 1200:
        parser.error("--duration-seconds must be between 1 and 1200")
    if os.geteuid() != 0 or socket.gethostname() != NODE:
        raise SystemExit("Run as root on the expected k3s VM.")
    path = Path(options.output)
    if not path.is_absolute():
        parser.error("--output must be an absolute path")
    os.umask(0o077)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "a") as output:
        if not stat.S_ISREG(os.fstat(output.fileno()).st_mode):
            raise SystemExit("Output must be a regular file.")
        fcntl.flock(output.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.fchmod(output.fileno(), 0o600)
        return observe(output, options.duration_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
