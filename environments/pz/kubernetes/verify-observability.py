#!/usr/bin/env python3
"""Read-only collector checks, intended to run as root on the existing k3s VM.

Example: python3 verify-observability.py --interval 90
No credentials, raw logs, configuration contents, or full kubelet responses are
printed. A temporary localhost-only port-forward works with shell-less images.
Exporter counters do not establish actual Monium query/dashboard visibility.
Exit 0 requires fresh kubelet collection and Monium metric acknowledgments from
the same Ready collector container; missing or stale evidence returns exit 2.
"""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import selectors
import subprocess
import time
import urllib.request


FAMILIES = (
    "receiver_accepted_metric_points", "receiver_refused_metric_points",
    "receiver_accepted_log_records", "receiver_refused_log_records",
    "receiver_failed_log_records", "exporter_sent_metric_points",
    "exporter_send_failed_metric_points", "exporter_enqueue_failed_metric_points",
    "exporter_sent_log_records", "exporter_send_failed_log_records",
    "exporter_enqueue_failed_log_records", "scraper_scraped_metric_points",
    "scraper_errored_metric_points", "exporter_queue_size", "exporter_queue_capacity",
)
SAFE_LABELS = {
    "receiver", "exporter", "processor", "scraper", "transport", "data_type",
    "otelcol_component_id", "otelcol_component_kind", "otelcol_signal",
}
SAMPLE = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*?)\})?\s+(\S+)(?:\s+\S+)?$')
LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"')


def read_metrics(text):
    result = {key: {"present": False, "total": None, "components": []} for key in FAMILIES}
    for line in text.splitlines():
        match = SAMPLE.match(line)
        if not match:
            continue
        name, raw_labels, raw_value = match.groups()
        if not name.startswith("otelcol_"):
            continue
        name = name[len("otelcol_"):]
        if name.endswith("_total"):
            name = name[:-len("_total")]
        if name not in result:
            continue
        try:
            value = float(raw_value)
        except ValueError:
            continue
        if not math.isfinite(value):
            continue
        labels = {}
        for key, escaped in LABEL.findall(raw_labels or ""):
            if key in SAFE_LABELS:
                # Prometheus permits only newline, quote and backslash escapes.
                labels[key] = re.sub(r'\\([n"\\])', lambda m: "\n" if m[1] == "n" else m[1], escaped)
        family = result[name]
        family["present"] = True
        family["total"] = (family["total"] or 0) + value
        if len(family["components"]) < 32:
            family["components"].append({"labels": labels, "value": value})
    return result


def component_total(families, family, label, value):
    samples = [row["value"] for row in families[family]["components"]
               if row["labels"].get(label) == value]
    return sum(samples) if samples else None


def collector_identity(pod):
    """A complete running-container identity; incomplete status is inconclusive."""
    if not isinstance(pod, dict):
        return None
    metadata, status = pod.get("metadata", {}), pod.get("status", {})
    if (metadata.get("deletionTimestamp") or status.get("phase") != "Running"
            or not any(c.get("type") == "Ready" and c.get("status") == "True"
                       for c in status.get("conditions", []))):
        return None
    containers = [c for c in status.get("containerStatuses", []) if c.get("name") == "otel-collector"]
    if len(containers) != 1 or not containers[0].get("ready"):
        return None
    container = containers[0]
    identity = (metadata.get("uid"), container.get("containerID"), container.get("restartCount"),
                container.get("state", {}).get("running", {}).get("startedAt"))
    return identity if all(value is not None and value != "" for value in identity) else None


def assess_samples(before, after, interval, stable_container):
    """Use metric/component names observed in the initial live collector report."""
    resets, fresh_errors = [], []
    for name, current in after.items():
        previous = before[name]
        current["delta"] = current["total"] - previous["total"] if current["present"] and previous["present"] else None
        if name.startswith("exporter_queue_"):
            continue  # Gauges can decrease normally.
        previous_components = {tuple(sorted(row["labels"].items())): row["value"] for row in previous["components"]}
        current_components = {tuple(sorted(row["labels"].items())): row["value"] for row in current["components"]}
        if (previous["present"] and not current["present"]
                or current["delta"] is not None and current["delta"] < 0
                or any(key not in current_components or current_components[key] < value
                       for key, value in previous_components.items())):
            resets.append(name)
        if any(word in name for word in ("failed", "refused", "errored")):
            # Failure counters may first appear when a failure actually occurs.
            # Their absence in the first sample must not hide a new nonzero one.
            if (any(value > previous_components.get(key, 0) for key, value in current_components.items())
                    or current["present"] and current["total"] > (previous["total"] or 0)):
                fresh_errors.append(name)

    def delta(family, label=None, value=None):
        old = component_total(before, family, label, value) if label else before[family]["total"]
        new = component_total(after, family, label, value) if label else after[family]["total"]
        return new - old if old is not None and new is not None else None

    deltas = {
        "accepted_metric_points": delta("receiver_accepted_metric_points"),
        "kubelet_accepted_metric_points": delta("receiver_accepted_metric_points", "receiver", "kubelet_stats"),
        "monium_sent_metric_points": delta("exporter_sent_metric_points", "exporter", "otlp_http/monium"),
        "accepted_log_records": delta("receiver_accepted_log_records"),
        "monium_sent_log_records": delta("exporter_sent_log_records", "exporter", "otlp_http/monium_logs"),
    }
    logs_arrived = (deltas["accepted_log_records"] or 0) > 0
    checks = {
        "window_at_least_90_seconds": interval >= 90,
        "same_ready_collector_container": stable_container,
        "required_metric_families_present_in_both_samples": all(
            before[name]["present"] and after[name]["present"]
            for name in ("receiver_accepted_metric_points", "exporter_sent_metric_points")),
        "fresh_metrics_accepted": (deltas["accepted_metric_points"] or 0) > 0,
        "fresh_kubelet_metrics_accepted": (deltas["kubelet_accepted_metric_points"] or 0) > 0,
        "fresh_monium_metrics_acknowledged": (deltas["monium_sent_metric_points"] or 0) > 0,
        "fresh_log_acknowledgment_if_logs_arrived": not logs_arrived or (deltas["monium_sent_log_records"] or 0) > 0,
        "no_counter_resets_or_disappearance": not resets,
        "no_fresh_receiver_scraper_or_exporter_errors": not fresh_errors,
    }
    return {"conclusive": all(checks.values()), "checks": checks, "component_deltas": deltas,
            "counter_resets_or_disappearances": resets, "fresh_error_families": fresh_errors,
            "log_delivery_required": logs_arrived}


def log_summary(text):
    """Count fixed error categories; never return original lines or messages."""
    categories = {
        "tls": r"x509|certificate|tls handshake",
        "authentication_or_rbac": r"unauthorized|forbidden|permission denied|unauthenticated|status.?code.?[:= ]+(401|403)",
        "network": r"connection refused|dial tcp|no such host|i/o timeout|context deadline exceeded",
        "queue_or_storage": r"queue.*full|no space left|database.*lock|file.?storage.*error|read.only file system",
        "memory": r"out of memory|memory limit|refus.*memory",
    }
    lines = text.splitlines()
    errors = [line for line in lines if re.search(r'\berror\b|\bfatal\b|\bwarn(?:ing)?\b|failed|failure', line, re.I)]
    return {
        "lines_examined": len(lines),
        "error_or_warning_lines": len(errors),
        "kubelet_error_or_warning_lines": sum(bool(re.search(r"kubelet[_ ]?stats|:10250|/stats/summary", line, re.I)) for line in errors),
        "categories": {key: sum(bool(re.search(pattern, line, re.I)) for line in errors) for key, pattern in categories.items()},
        "raw_logs_omitted": True,
    }


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def execute(command, timeout=20):
    try:
        result = subprocess.run(command, text=True, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": "timeout"}, ""
    except OSError:
        return {"ok": False, "reason": "executable_unavailable"}, ""
    if result.returncode:
        return {"ok": False, "reason": "command_failed", "exit_code": result.returncode,
                "error_categories": log_summary(result.stderr)["categories"]}, result.stdout + result.stderr
    return {"ok": True}, result.stdout


def get_json(command):
    status, output = execute(command)
    if not status["ok"]:
        return status, None
    try:
        return status, json.loads(output)
    except json.JSONDecodeError:
        return {"ok": False, "reason": "invalid_json"}, None


@contextmanager
def port_forward(kubectl, namespace, pod):
    command = kubectl + ["-n", namespace, "port-forward", "--address=127.0.0.1", "pod/" + pod, "0:8888"]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    try:
        deadline = time.monotonic() + 15
        port = None
        while time.monotonic() < deadline and process.poll() is None:
            if not selector.select(timeout=0.5):
                continue
            line = process.stdout.readline()
            match = re.search(r"Forwarding from 127\.0\.0\.1:(\d+) -> 8888", line)
            if match:
                port = int(match[1])
                break
        if port is None:
            raise RuntimeError("localhost_port_forward_unavailable")
        yield f"http://127.0.0.1:{port}/metrics"
    finally:
        selector.close()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        process.stdout.close()


def fetch_metrics(url):
    # Always reach the local tunnel directly, regardless of root's proxy env.
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url, timeout=8) as response:
        raw = response.read(4 * 1024 * 1024 + 1)
    if len(raw) > 4 * 1024 * 1024:
        raise RuntimeError("collector_metrics_exceed_4MiB_bound")
    return read_metrics(raw.decode("utf-8", errors="replace"))


def kubelet_summary(document):
    selected = []
    for pod in document.get("pods", []):
        reference = pod.get("podRef", {})
        if reference.get("namespace") not in {"zomboid", "edge", "observability"}:
            continue
        for container in pod.get("containers", []):
            if len(selected) == 12:
                break
            selected.append({
                "namespace": reference.get("namespace"), "pod": reference.get("name"),
                "container": container.get("name"), "start_time": container.get("startTime"),
                "cpu_usage_nanocores": container.get("cpu", {}).get("usageNanoCores"),
                "memory_usage_bytes": container.get("memory", {}).get("usageBytes"),
                "memory_working_set_bytes": container.get("memory", {}).get("workingSetBytes"),
            })
    return {"selected_container_samples": selected,
            "scope": "Kubelet source via administrator API proxy; this does not test the collector service-account authorization or Monium readback."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubectl", default="/usr/local/bin/k3s", help="Path to k3s or kubectl; no shell expression.")
    parser.add_argument("--kubeconfig", default="/etc/rancher/k3s/operator.yaml")
    parser.add_argument("--namespace", default="observability")
    parser.add_argument("--interval", type=int, default=90, help="Seconds between two internal metric samples, 90–120; kubelet collection runs every 60 seconds.")
    parser.add_argument("--since", default="15m", help="Bounded recent log interval, for example 15m.")
    parser.add_argument("--skip-validation", action="store_true")
    args = parser.parse_args()
    if not 90 <= args.interval <= 120 or not re.fullmatch(r"[1-9][0-9]?[smh]", args.since):
        parser.error("interval must be 90–120 seconds and since a duration such as 15m (maximum 99 units).")
    kubectl = [args.kubectl] + (["kubectl"] if Path(args.kubectl).name == "k3s" else [])
    kubectl += ["--kubeconfig", args.kubeconfig, "--request-timeout=15s"]
    scoped = kubectl + ["-n", args.namespace]
    report = {"started_at": utc_now(), "read_only": True,
              "monium_readback": "not_verified; exporter acknowledgments do not prove query/dashboard visibility"}
    status, document = get_json(scoped + ["get", "pods", "-l", "app.kubernetes.io/name=otel-collector", "-o", "json"])
    report["pod_lookup"] = status
    if document is None:
        print(json.dumps(report, ensure_ascii=False, indent=2)); return 2
    pods = document.get("items", [])
    report["pods"] = [{"name": pod["metadata"]["name"], "phase": pod.get("status", {}).get("phase"),
                       "terminating": bool(pod["metadata"].get("deletionTimestamp")),
                       "ready": any(c.get("type") == "Ready" and c.get("status") == "True" for c in pod.get("status", {}).get("conditions", [])),
                       "restarts": sum(c.get("restartCount", 0) for c in pod.get("status", {}).get("containerStatuses", []))}
                      for pod in pods]
    active = [pod for pod in pods if not pod["metadata"].get("deletionTimestamp") and pod.get("status", {}).get("phase") == "Running"]
    if len(active) != 1:
        report["attention"] = "Expected exactly one running collector; no metrics sampling attempted."
        print(json.dumps(report, ensure_ascii=False, indent=2)); return 2
    pod = active[0]
    name = pod["metadata"]["name"]
    report["image"] = next((c.get("image") for c in pod["spec"].get("containers", []) if c.get("name") == "otel-collector"), None)
    logs_status, logs = execute(scoped + ["logs", name, "-c", "otel-collector", "--since=" + args.since, "--tail=200", "--limit-bytes=131072"])
    report["recent_logs"] = {**logs_status, **log_summary(logs)}
    if not args.skip_validation:
        validation, output = execute(scoped + ["exec", name, "-c", "otel-collector", "--", "/otelcol-contrib", "validate", "--config=/etc/otelcol/config.yaml"])
        if not validation["ok"] and re.search(r"unknown command|executable file not found|no such file or directory", output, re.I):
            validation["reason"] = "validation_command_unavailable"
        report["configuration_validation"] = validation
    else:
        report["configuration_validation"] = {"skipped": True}
    try:
        with port_forward(kubectl, args.namespace, name) as url:
            before = fetch_metrics(url)
            time.sleep(args.interval)
            after = fetch_metrics(url)
        final_status, final_pod = get_json(scoped + ["get", "pod", name, "-o", "json"])
        initial_identity = collector_identity(pod)
        stable = bool(initial_identity is not None and final_status["ok"]
                      and collector_identity(final_pod) == initial_identity)
        report["final_pod_lookup"] = final_status
        report["sampling_assessment"] = assess_samples(before, after, args.interval, stable)
        report["collector_metrics"] = {"ok": True, "interval_seconds": args.interval, "families": after,
            "counter_note": "Values are process-lifetime totals; nonzero failure totals may be historical. Missing families are not zero. Queue totals mix component units; inspect data_type components."}
        report["delivery_observation"] = {}
        for signal, family in [("metrics", "exporter_sent_metric_points"), ("logs", "exporter_sent_log_records")]:
            sample = after[family]
            report["delivery_observation"][signal] = (
                "acknowledged_during_sample_window" if (sample["delta"] or 0) > 0 else
                "acknowledged_since_collector_start" if (sample["total"] or 0) > 0 else
                "not_observed"
            )
        report["kubelet_receiver_samples"] = [
            {"family": key, **component}
            for key, family in after.items() for component in family["components"]
            if any("kubelet" in str(value).lower() for value in component["labels"].values())
        ]
    except Exception as error:
        # Exception text can contain server/proxy data, so emit only fixed type.
        report["collector_metrics"] = {"ok": False, "reason": type(error).__name__}
    node = pod.get("spec", {}).get("nodeName", "")
    if node and re.fullmatch(r"[a-z0-9.-]+", node):
        status, document = get_json(kubectl + ["get", "--raw", f"/api/v1/nodes/{node}/proxy/stats/summary"])
        report["kubelet_source"] = {**status, **(kubelet_summary(document) if document else {})}
    report["completed_at"] = utc_now()
    unhealthy = not all(row["ready"] for row in report["pods"] if not row["terminating"])
    metrics = report["collector_metrics"]
    attention = unhealthy or not metrics["ok"] or report["configuration_validation"].get("ok") is False
    if metrics["ok"]:
        attention |= not report["sampling_assessment"]["conclusive"]
    report["attention_required"] = bool(attention)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 2 if attention else 0


if __name__ == "__main__":
    raise SystemExit(main())
