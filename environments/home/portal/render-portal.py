#!/usr/bin/env python3
"""Render dormant Portal workloads from the captured production image digests."""

import copy
import json
from pathlib import Path


NAMESPACE = "updspace-portal"
PART_OF = {"app.kubernetes.io/part-of": NAMESPACE}
OUTBOX_COMMANDS = {
    "activity": "process_outbox",
    "events": "publish_outbox",
    "featureflags": "publish_outbox",
    "gamification": "publish_outbox",
    "portal": "process_outbox",
    "voting": "publish_outbox",
}


def render() -> dict:
    images = json.loads(Path(__file__).with_name("portal-local-images.json").read_text())
    items = [{"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": NAMESPACE}}]
    for name, image in images.items():
        labels = {**PART_OF, "app.kubernetes.io/name": name}
        container = {
            "name": "app",
            "image": image,
            "imagePullPolicy": "Never",
            "envFrom": [{"secretRef": {"name": name + "-runtime"}}],
            "resources": {
                "requests": {"cpu": "50m", "memory": "128Mi"},
                "limits": {"cpu": "1", "memory": "512Mi"},
            },
            "securityContext": {
                "allowPrivilegeEscalation": False,
                "readOnlyRootFilesystem": True,
                "capabilities": {"drop": ["ALL"]},
            },
            "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"}],
        }
        pod = {
            "automountServiceAccountToken": False,
            "securityContext": {"seccompProfile": {"type": "RuntimeDefault"}},
            "terminationGracePeriodSeconds": 60,
            "containers": [container],
            "volumes": [{"name": "tmp", "emptyDir": {"sizeLimit": "128Mi"}}],
        }
        app_pod = copy.deepcopy(pod)
        app_pod["containers"][0].update({
            "command": [
                "gunicorn", "app.wsgi:application", "--bind", "0.0.0.0:8000",
                "--workers", "1", "--worker-class", "gthread", "--threads", "8",
                "--worker-tmp-dir", "/tmp", "--access-logfile", "-",
            ],
            "ports": [{"name": "http", "containerPort": 8000}],
            "startupProbe": {
                "httpGet": {"path": "/health", "port": "http", "httpHeaders": [
                    {"name": "Host", "value": "localhost"},
                    {"name": "X-Forwarded-Proto", "value": "https"},
                ]},
                "periodSeconds": 5, "failureThreshold": 30,
            },
            "readinessProbe": {
                "httpGet": {"path": "/health", "port": "http", "httpHeaders": [
                    {"name": "Host", "value": "localhost"},
                    {"name": "X-Forwarded-Proto", "value": "https"},
                ]},
                "periodSeconds": 10, "timeoutSeconds": 3,
            },
        })
        metadata = {"name": name, "namespace": NAMESPACE}
        items.extend([
            {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": metadata,
             "spec": {"replicas": 0, "selector": {"matchLabels": labels},
                      "template": {"metadata": {"labels": labels}, "spec": app_pod}}},
            {"apiVersion": "v1", "kind": "Service", "metadata": metadata,
             "spec": {"selector": labels, "ports": [
                 {"name": "http", "port": 8000, "targetPort": "http"}]}},
        ])
        jobs = {"retention": "prune_outbox" if name == "featureflags" else "purge_retention"}
        if name in OUTBOX_COMMANDS:
            jobs["outbox"] = OUTBOX_COMMANDS[name]
        for kind, command in jobs.items():
            job_pod = copy.deepcopy(pod)
            job_pod["restartPolicy"] = "Never"
            job_pod["containers"][0]["command"] = ["python", "src/manage.py", command]
            items.append({
                "apiVersion": "batch/v1", "kind": "CronJob",
                "metadata": {"name": name + "-" + kind, "namespace": NAMESPACE},
                "spec": {
                    "schedule": "* * * * *" if kind == "outbox" else "30 3 * * *",
                    "timeZone": "Etc/UTC", "suspend": True,
                    "concurrencyPolicy": "Forbid", "startingDeadlineSeconds": 60,
                    "successfulJobsHistoryLimit": 1, "failedJobsHistoryLimit": 2,
                    "jobTemplate": {"spec": {
                        "backoffLimit": 0, "activeDeadlineSeconds": 300,
                        "template": {"metadata": {"labels": labels}, "spec": job_pod},
                    }},
                },
            })
    return {"apiVersion": "v1", "kind": "List", "items": items}


if __name__ == "__main__":
    manifest = render()
    deployments = [x for x in manifest["items"] if x["kind"] == "Deployment"]
    jobs = [x for x in manifest["items"] if x["kind"] == "CronJob"]
    assert len(deployments) == 8 and len(jobs) == 14
    assert all(x["spec"]["replicas"] == 0 for x in deployments)
    assert all(x["spec"]["suspend"] and x["spec"]["concurrencyPolicy"] == "Forbid" for x in jobs)
    assert all("@sha256:" in x["spec"]["template"]["spec"]["containers"][0]["image"] for x in deployments)
    print(json.dumps(manifest, indent=2))
