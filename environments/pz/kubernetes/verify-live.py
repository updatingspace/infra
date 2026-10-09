#!/usr/bin/env python3
"""Read-only acceptance checks for this single-node PZ migration.

Run as root ON THE VM after rollout:
  python3 verify-live.py --wait-ready-seconds 1800 > verification.json
After retiring the legacy Docker runtime (never contacts its socket):
  python3 verify-live.py --legacy-retired > verification.json

stdout is one allowlisted JSON object; exit 0 means every required check passed.
No environment, Secret data, full pod specification, kubeconfig, or command stderr
is printed. This does not prove external UDP/HTTPS reachability or Monium delivery.
It never changes Kubernetes objects, Docker containers, files, or mounts. The sole
exec inside an application pod reads its effective UID using Python.
"""
import argparse
import datetime
from decimal import Decimal
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import sys
import time


NODE = "compute-vm-2-6-60-ssd-1785610759198"
KUBECONFIG = "/etc/rancher/k3s/operator.yaml"
MIB = 1024 ** 2
GIB = 1024 ** 3
BUDGET_CPU_M = 3000
BUDGET_MEMORY_MIB = 12000
EXPECTED_QUOTAS = {
    "zomboid": {
        "requests.cpu": "2000m", "limits.cpu": "2600m",
        "requests.memory": "8512Mi", "limits.memory": "11136Mi",
        "requests.ephemeral-storage": "2048Mi", "limits.ephemeral-storage": "4096Mi",
        "requests.storage": "26Gi", "persistentvolumeclaims": "8", "pods": "6",
    },
    "edge": {
        "requests.cpu": "100m", "limits.cpu": "200m",
        "requests.memory": "128Mi", "limits.memory": "256Mi",
        "requests.ephemeral-storage": "128Mi", "limits.ephemeral-storage": "512Mi",
        "requests.storage": "256Mi", "persistentvolumeclaims": "2", "pods": "2",
    },
    "observability": {
        "requests.cpu": "100m", "limits.cpu": "200m",
        "requests.memory": "256Mi", "limits.memory": "512Mi",
        "requests.ephemeral-storage": "256Mi", "limits.ephemeral-storage": "1024Mi",
        "requests.storage": "512Mi", "persistentvolumeclaims": "2", "pods": "2",
    },
}
FILESYSTEMS = {
    "zomboid": {"size_mib": 26624, "directories": ["pz-server", "zomboid", "steam", "panel", "panel-logs"]},
    "edge": {"size_mib": 256, "directories": ["caddy-data", "caddy-config"]},
    "observability": {"size_mib": 512, "directories": ["otelcol"]},
}
CLAIMS = {
    "pz-server": ("zomboid", "11Gi"), "zomboid": ("zomboid", "12Gi"),
    "steam": ("zomboid", "1Gi"), "panel": ("zomboid", "1Gi"),
    "panel-logs": ("zomboid", "1Gi"),
    "caddy-data": ("edge", "128Mi"), "caddy-config": ("edge", "128Mi"),
}
APPS = {"zomboid": "zomboid", "panel": "zomboid", "caddy": "edge", "otel-collector": "observability"}
APP_LIMITS = {
    "zomboid": {"cpu": "2200m", "memory": "10Gi"},
    "panel": {"cpu": "300m", "memory": "768Mi"},
    "caddy": {"cpu": "200m", "memory": "256Mi"},
    "otel-collector": {"cpu": "200m", "memory": "512Mi"},
}
SECRETS = {"zomboid": {"pz-runtime", "pz-panel"}, "edge": {"caddy-runtime"}, "observability": {"monium-env"}}
TERMINAL_PHASES = {"Succeeded", "Failed"}
LEGACY_UNITS = ("docker.service", "docker.socket", "containerd.service")
DOCKER_CONTAINERS = Path("/var/lib/docker/containers")
DISK_MIGRATION_JOURNAL = Path("/var/lib/pz-backup/disk-migration/journal.json")
BACKUP_CONFIG = Path("/etc/pz-backup/config.json")
DATA_MOUNT = Path("/srv/pz-storage/zomboid")
OLD_DATA_MOUNT = Path("/srv/pz-storage/zomboid-old")
UUID_PATTERN = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.IGNORECASE)


def trusted_private_json(path):
    """Bounded root-owned operator evidence; never follow a substituted file."""
    for parent in path.parents:
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError("Untrusted evidence directory")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > MIB):
            raise ValueError("Untrusted evidence file")
        content = handle.read(MIB + 1)
    if len(content) > MIB:
        raise ValueError("Oversized evidence")
    value = json.loads(content)
    if not isinstance(value, dict):
        raise ValueError("Evidence is not an object")
    return value


def data_disk_migration():
    if not os.path.lexists(DISK_MIGRATION_JOURNAL):
        return None
    journal = trusted_private_json(DISK_MIGRATION_JOURNAL)
    config = trusted_private_json(BACKUP_CONFIG)
    if (journal.get("format") != "pz-disk-migration-v1" or journal.get("phase") != "complete"
            or not all(isinstance(journal.get(key), str) and UUID_PATTERN.fullmatch(journal[key])
                       for key in ("old_uuid", "new_uuid"))
            or journal["old_uuid"].lower() == journal["new_uuid"].lower()
            or not isinstance(journal.get("disk_id"), str)
            or not re.fullmatch(r"[a-z0-9]{20}", journal["disk_id"])
            or journal.get("old_copy_path") != str(OLD_DATA_MOUNT)
            or config.get("data_root") != str(DATA_MOUNT)
            or not isinstance(config.get("data_uuid"), str)
            or config["data_uuid"].lower() != journal["new_uuid"].lower()):
        raise ValueError("Incomplete or inconsistent disk migration")
    completed = datetime.datetime.fromisoformat(journal["completed_at"].replace("Z", "+00:00"))
    if (completed.tzinfo is None or completed.utcoffset() != datetime.timedelta(0)
            or completed > datetime.datetime.now(datetime.timezone.utc)):
        raise ValueError("Invalid migration completion time")
    return journal, config


def quantity(value):
    """Kubernetes Quantity -> Decimal base units; never use binary float."""
    match = re.fullmatch(r"([+-]?(?:\d+(?:\.\d*)?|\.\d+))([eE][+-]?\d+|[numkKMGTEP]i?|Ki)?", str(value))
    if not match:
        raise ValueError("Unsupported resource quantity")
    number, suffix = Decimal(match[1]), match[2] or ""
    if suffix[:1] in {"e", "E"} and len(suffix) > 1:
        return number * Decimal(10) ** int(suffix[1:])
    scale = {
        "": 1, "n": Decimal("1e-9"), "u": Decimal("1e-6"), "m": Decimal("0.001"),
        "k": 1000, "K": 1000, "M": 1000 ** 2, "G": 1000 ** 3,
        "T": 1000 ** 4, "P": 1000 ** 5, "E": 1000 ** 6,
        "Ki": 1024, "Mi": MIB, "Gi": GIB, "Ti": 1024 ** 4, "Pi": 1024 ** 5, "Ei": 1024 ** 6,
    }
    return number * scale[suffix]


class CommandFailure(Exception):
    def __init__(self, label, code):
        self.label, self.code = label, code


def capture(argv, label, timeout=45, empty_status=None):
    try:
        result = subprocess.run(argv, text=True, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        raise CommandFailure(label, "timeout") from None
    except OSError:
        raise CommandFailure(label, "unavailable") from None
    if result.returncode:
        # findmnt --mountpoint returns 1 with no output when that exact path is
        # not mounted. Any diagnostic or partial response remains an error.
        if result.returncode == empty_status and not result.stdout.strip() and not result.stderr.strip():
            return ""
        raise CommandFailure(label, result.returncode)
    return result.stdout.strip()


def systemd_unit(unit):
    fields = ("LoadState", "ActiveState", "SubState", "UnitFileState")
    output = capture(["systemctl", "show", "--no-pager", unit,
                      *["--property=" + field for field in fields]], "systemctl show " + unit)
    properties = {}
    for line in output.splitlines():
        key, separator, value = line.partition("=")
        if not separator or key not in fields or key in properties:
            raise ValueError("Unexpected systemd property response")
        properties[key] = value
    if set(properties) != set(fields):
        raise ValueError("Incomplete systemd property response")
    return properties


def kubectl(*args):
    return capture(["/usr/local/bin/k3s", "kubectl", "--kubeconfig", KUBECONFIG,
                    "--request-timeout=30s", *args], "kubectl " + args[0])


def get_items(kind):
    return json.loads(kubectl("get", kind, "-A", "-o", "json"))["items"]


def pod_ready(pod):
    status = pod.get("status", {})
    containers = status.get("containerStatuses", [])
    return (status.get("phase") == "Running" and not pod["metadata"].get("deletionTimestamp")
            and any(c.get("type") == "Ready" and c.get("status") == "True" for c in status.get("conditions", []))
            and bool(containers) and all(c.get("ready", False) for c in containers))


def active(pods):
    return [p for p in pods if p.get("status", {}).get("phase") not in TERMINAL_PHASES]


def app_pods(pods, name, namespace):
    return [p for p in active(pods) if p["metadata"].get("namespace") == namespace
            and p["metadata"].get("labels", {}).get("app.kubernetes.io/name") == name]


def service_lb_pods(pods, service):
    return [p for p in active(pods) if p["metadata"].get("namespace") == "kube-system"
            and p["metadata"].get("name", "").startswith("svclb-" + service + "-")]


def rollout_ready(pods):
    groups = [app_pods(pods, name, namespace) for name, namespace in APPS.items()]
    groups += [service_lb_pods(pods, name) for name in ("zomboid-public", "caddy")]
    return all(len(group) == 1 and pod_ready(group[0]) for group in groups)


class Verification:
    def __init__(self, options):
        self.options = options
        self.report = {
            "checked_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "read_only": True, "expected_node": NODE, "checks": [], "failures": [],
            "limits_of_verification": ["External game UDP and HTTPS are not tested", "Monium ingestion and alert evaluation are not tested"],
        }
        self.pods = []

    def check(self, name, condition):
        passed = bool(condition)
        self.report["checks"].append({"name": name, "passed": passed})
        if not passed:
            self.report["failures"].append(name)
        return passed

    def section(self, name, function):
        try:
            function()
        except CommandFailure as error:
            self.report.setdefault("command_errors", []).append({"section": name, "command": error.label, "result": error.code})
            self.check(name + ": command succeeded", False)
        except Exception as error:
            # Exception messages can include API objects or paths with values;
            # only the exception type is included in the public report.
            self.report.setdefault("section_errors", []).append({"section": name, "type": type(error).__name__})
            self.check(name + ": data could be verified", False)

    def cluster(self):
        nodes = get_items("nodes")
        self.check("exactly one expected node", len(nodes) == 1 and nodes[0]["metadata"]["name"] == NODE)
        self.report["nodes"] = []
        for node in nodes:
            status = node.get("status", {})
            conditions = {c["type"]: c["status"] for c in status.get("conditions", [])}
            allocatable = status.get("allocatable", {})
            self.report["nodes"].append({
                "name": node["metadata"]["name"], "allocatable": allocatable,
                "capacity": status.get("capacity", {}), "conditions": conditions,
                "kubelet_version": status.get("nodeInfo", {}).get("kubeletVersion"),
            })
            self.check("node Ready", conditions.get("Ready") == "True")
            self.check("no node memory/disk/PID pressure", all(conditions.get(key) == "False" for key in ("MemoryPressure", "DiskPressure", "PIDPressure")))
            self.check("node allocatable covers application CPU", quantity(allocatable.get("cpu", "0")) * 1000 >= BUDGET_CPU_M)
            self.check("node allocatable covers application RAM", quantity(allocatable.get("memory", "0")) >= BUDGET_MEMORY_MIB * MIB)
        deadline = time.monotonic() + self.options.wait_ready_seconds
        while True:
            self.pods = get_items("pods")
            if rollout_ready(self.pods) or time.monotonic() >= deadline:
                break
            time.sleep(min(10, max(0, deadline - time.monotonic())))
        self.report["readiness_wait_seconds"] = self.options.wait_ready_seconds
        self.report["pods"] = [{
            "namespace": p["metadata"].get("namespace"), "name": p["metadata"]["name"],
            "node": p.get("spec", {}).get("nodeName"), "phase": p.get("status", {}).get("phase"), "ready": pod_ready(p),
            "restarts": {c["name"]: c.get("restartCount", 0) for c in p.get("status", {}).get("containerStatuses", [])},
        } for p in self.pods]
        for name, namespace in APPS.items():
            group = app_pods(self.pods, name, namespace)
            self.check(name + ": exactly one Ready pod", len(group) == 1 and pod_ready(group[0]))
            for pod in group:
                self.check(name + ": on expected node", pod.get("spec", {}).get("nodeName") == NODE)
        for service in ("zomboid-public", "caddy"):
            group = service_lb_pods(self.pods, service)
            self.check(service + ": one Ready ServiceLB pod in kube-system", len(group) == 1 and pod_ready(group[0]))
        system = [p for p in active(self.pods) if p["metadata"].get("namespace") == "kube-system"]
        self.check("all active kube-system pods Ready", bool(system) and all(pod_ready(p) for p in system))
        self.check("CoreDNS Ready", any(p["metadata"].get("labels", {}).get("k8s-app") == "kube-dns" and pod_ready(p) for p in system))
        self.check("no unexpected application namespace", all(p["metadata"].get("namespace") in {*EXPECTED_QUOTAS, "kube-system"} for p in active(self.pods)))
        self.check("no Failed application pods", not any(p["metadata"].get("namespace") in EXPECTED_QUOTAS and p.get("status", {}).get("phase") == "Failed" for p in self.pods))

    def quotas(self):
        items = get_items("resourcequotas")
        self.report["resourcequotas"] = []
        for namespace, expected in EXPECTED_QUOTAS.items():
            matches = [q for q in items if q["metadata"].get("namespace") == namespace and q["metadata"]["name"] == "environment-budget"]
            if not self.check(namespace + ": environment quota exists", len(matches) == 1):
                continue
            quota = matches[0]
            hard, used = quota.get("spec", {}).get("hard", {}), quota.get("status", {}).get("used", {})
            self.report["resourcequotas"].append({"namespace": namespace, "hard": hard, "used": used})
            self.check(namespace + ": exact quota values", all(key in hard and quantity(hard[key]) == quantity(value) for key, value in expected.items()))
            self.check(namespace + ": quota usage <= hard", all(quantity(value) <= quantity(hard[key]) for key, value in used.items() if key in hard))
        self.report["application_budget"] = {"cpu_m": BUDGET_CPU_M, "memory_mib": BUDGET_MEMORY_MIB, "namespace_limits_cpu_m": 3000, "namespace_limits_memory_mib": 11904}

    def claims(self):
        claims, volumes = get_items("persistentvolumeclaims"), get_items("persistentvolumes")
        self.report["persistentvolumeclaims"] = []
        for name, (namespace, requested) in CLAIMS.items():
            found = [c for c in claims if c["metadata"]["name"] == name and c["metadata"].get("namespace") == namespace]
            if not self.check(namespace + "/" + name + ": PVC exists", len(found) == 1):
                continue
            claim = found[0]
            spec, status = claim.get("spec", {}), claim.get("status", {})
            self.report["persistentvolumeclaims"].append({"namespace": namespace, "name": name, "phase": status.get("phase"), "volume": spec.get("volumeName"), "requested": spec.get("resources", {}).get("requests", {}).get("storage"), "capacity": status.get("capacity", {}).get("storage")})
            self.check(name + ": PVC Bound to expected PV", status.get("phase") == "Bound" and spec.get("volumeName") == "pz-" + name)
            self.check(name + ": exact PVC request", quantity(spec.get("resources", {}).get("requests", {}).get("storage", "0")) == quantity(requested))
            pvs = [p for p in volumes if p["metadata"]["name"] == "pz-" + name]
            if not self.check(name + ": PV exists", len(pvs) == 1):
                continue
            pv = pvs[0].get("spec", {})
            self.check(name + ": PV Retain and expected local path", pv.get("persistentVolumeReclaimPolicy") == "Retain" and pv.get("local", {}).get("path") == "/opt/pz-stack/data/" + name)
            self.check(name + ": PV bound to exact claim UID", pv.get("claimRef", {}).get("uid") == claim["metadata"].get("uid"))
            self.check(name + ": exact PV capacity", quantity(pv.get("capacity", {}).get("storage", "0")) == quantity(requested))

    def dedicated_data_disk(self, record, image, migration):
        journal, _ = migration
        image_info = image.lstat()
        self.check("zomboid: preserved image remains root-owned and protected",
                   image_info.st_uid == 0 and image_info.st_nlink == 1 and not image_info.st_mode & 0o022)
        device = (Path("/dev/disk/by-id") / ("virtio-" + journal["disk_id"])).resolve(strict=True)
        if not self.check("zomboid: dedicated block device matches migration identity",
                          stat.S_ISBLK(device.stat().st_mode)
                          and Path(record["source"]).resolve(strict=True) == device
                          and str(record.get("uuid", "")).lower() == journal["new_uuid"].lower()):
            raise ValueError("Dedicated device identity mismatch")
        rows = json.loads(capture(["lsblk", "--json", "--bytes", "--output", "PATH,TYPE,SERIAL,SIZE,FSTYPE",
                                   str(device)], "lsblk data identity"))["blockdevices"]
        if not self.check("zomboid: whole ext4 disk has expected serial",
                          len(rows) == 1 and rows[0].get("path") == str(device)
                          and rows[0].get("type") == "disk" and not rows[0].get("children")
                          and rows[0].get("serial") == journal["disk_id"] and rows[0].get("fstype") == "ext4"
                          and int(rows[0].get("size", 0)) > 0):
            raise ValueError("Dedicated disk metadata mismatch")
        disk_uuid = capture(["blkid", "-p", "-s", "UUID", "-o", "value", str(device)], "blkid data identity")
        self.check("zomboid: device superblock UUID matches completed migration",
                   disk_uuid.lower() == journal["new_uuid"].lower())
        fs_options = set(record.get("fs-options", "").split(","))
        self.check("zomboid: dedicated ext4 superblock writable", "rw" in fs_options and "ro" not in fs_options)

        loops = json.loads(capture(["losetup", "--json", "--output", "NAME,BACK-FILE,OFFSET,SIZELIMIT,RO"],
                                   "losetup old data"))["loopdevices"]
        if not isinstance(loops, list) or any(not isinstance(loop, dict)
                                             or not isinstance(loop.get("back-file"), str)
                                             or not loop["back-file"] for loop in loops):
            raise ValueError("Cannot identify all attached loop images")
        matches = [loop for loop in loops if isinstance(loop.get("back-file"), str)
                   and Path(loop["back-file"]).resolve() == image.resolve()]
        image_uuid = capture(["blkid", "-p", "-s", "UUID", "-o", "value", str(image)], "blkid old data")
        self.check("zomboid: preserved image UUID matches original migration source",
                   image_uuid.lower() == journal["old_uuid"].lower())
        result = {"layout": "dedicated-disk", "image_role": "preserved-old-copy", "disk_id": journal["disk_id"],
                  "disk_size_bytes": int(rows[0]["size"]), "old_uuid": journal["old_uuid"]}
        old_response = capture(["findmnt", "--json", "--mountpoint", str(OLD_DATA_MOUNT), "-o",
                                "SOURCE,FSTYPE,UUID,TARGET,OPTIONS,FS-OPTIONS"], "findmnt old data", empty_status=1)
        if not old_response:
            # The old mount is intentionally not in fstab. After reboot the
            # protected original image may remain offline, with no loop users.
            self.check("zomboid: unmounted preserved image has no attached loops", not matches)
            return {**result, "old_copy_state": "unmounted-preserved", "old_mount": None}
        old_records = json.loads(old_response)["filesystems"]
        if len(old_records) != 1:
            raise ValueError("Preserved old mount is ambiguous")
        old = old_records[0]
        if not self.check("zomboid: preserved old ext4 loop is read-only",
                          old.get("target") == str(OLD_DATA_MOUNT) and old.get("fstype") == "ext4"
                          and str(old.get("uuid", "")).lower() == journal["old_uuid"].lower()
                          and re.fullmatch(r"/dev/loop[0-9]+", old.get("source", ""))
                          and {"ro", "nodev", "nosuid"} <= set(old.get("options", "").split(","))
                          and "rw" not in old.get("options", "").split(",")
                          and "ro" in old.get("fs-options", "").split(",")
                          and "rw" not in old.get("fs-options", "").split(",")):
            raise ValueError("Preserved old filesystem identity mismatch")
        self.check("zomboid: original backing image has one exact read-only loop",
                   len(matches) == 1 and matches[0].get("name") == old["source"]
                   and matches[0].get("back-file") == str(image)
                   and matches[0].get("offset") == 0 and matches[0].get("sizelimit") == 0
                   and matches[0].get("ro") is True)
        return {**result, "old_copy_state": "mounted-read-only", "old_mount": str(OLD_DATA_MOUNT)}

    def filesystems(self):
        self.report["filesystems"] = []
        migration = data_disk_migration()
        for namespace, spec in FILESYSTEMS.items():
            image, mount = Path("/var/lib/pz-volumes") / (namespace + ".ext4"), Path("/srv/pz-storage") / namespace
            image_stat = image.lstat()
            if not self.check(namespace + ": regular backing image with exact logical size", stat.S_ISREG(image_stat.st_mode) and image_stat.st_size == spec["size_mib"] * MIB):
                continue
            records = json.loads(capture(["findmnt", "--json", "--mountpoint", str(mount), "-o", "SOURCE,FSTYPE,UUID,TARGET,OPTIONS,FS-OPTIONS"], "findmnt"))["filesystems"]
            if not self.check(namespace + ": exactly one mounted filesystem", len(records) == 1):
                continue
            record = records[0]
            self.check(namespace + ": ext4 mounted at exact path", record.get("fstype") == "ext4" and record.get("target") == str(mount) and bool(record.get("uuid")))
            if namespace == "zomboid" and migration is not None:
                layout = self.dedicated_data_disk(record, image, migration)
            else:
                backing = capture(["losetup", "--noheadings", "--raw", "--output", "BACK-FILE", record["source"]], "losetup")
                self.check(namespace + ": mounted loop uses exact backing image", Path(backing).resolve() == image.resolve())
                image_uuid = capture(["blkid", "-p", "-s", "UUID", "-o", "value", str(image)], "blkid")
                self.check(namespace + ": mount UUID matches image", record.get("uuid") == image_uuid)
                layout = {"layout": "legacy-loop"}
            options = set(record.get("options", "").split(","))
            self.check(namespace + ": filesystem writable with nodev/nosuid", {"rw", "nodev", "nosuid"} <= options)
            usage = os.statvfs(mount)
            available = usage.f_bavail * usage.f_frsize
            total = usage.f_blocks * usage.f_frsize
            self.report["filesystems"].append({"namespace": namespace, "image": str(image), "mount": str(mount), "source": record.get("source"), "uuid": record.get("uuid"), "logical_size_bytes": image_stat.st_size, "backing_allocated_bytes": image_stat.st_blocks * 512, "filesystem_size_bytes": total, "available_bytes": available, **layout})
            self.check(namespace + ": filesystem has free space", available > 0 and total > 0)
            if layout["layout"] == "dedicated-disk":
                self.check(namespace + ": filesystem fits dedicated disk", total <= layout["disk_size_bytes"])
            for name in spec["directories"]:
                legacy, target = Path("/opt/pz-stack/data") / name, mount / name
                self.check(name + ": legacy path links to mounted data", legacy.is_symlink() and legacy.resolve() == target and target.is_dir())
        root = os.statvfs("/")
        free = root.f_bavail * root.f_frsize
        self.report["root_filesystem"] = {"size_bytes": root.f_blocks * root.f_frsize, "available_bytes": free, "required_free_bytes": self.options.minimum_root_free_mib * MIB}
        self.check("physical root filesystem has required reserve", free >= self.options.minimum_root_free_mib * MIB)
        self.check("disk migration evidence remained unchanged during verification", data_disk_migration() == migration)

    def legacy(self):
        if getattr(self.options, "legacy_retired", False):
            self.retired_legacy()
            return
        self.report["legacy_mode"] = "migration"
        ids = capture(["docker", "ps", "-aq", "--filter", "label=com.docker.compose.project=pz-b42", "--filter", "label=com.docker.compose.oneoff=False"], "docker container IDs").split()
        self.report["legacy_containers"] = []
        found = set()
        template = ('{"id":{{json .Id}},"name":{{json .Name}},'
                    '"service":{{json (index .Config.Labels "com.docker.compose.service")}},'
                    '"running":{{json .State.Running}},"paused":{{json .State.Paused}},'
                    '"restarting":{{json .State.Restarting}},"status":{{json .State.Status}},'
                    '"exit_code":{{json .State.ExitCode}},"restart_policy":{{json .HostConfig.RestartPolicy.Name}}}')
        for identifier in ids:
            data = json.loads(capture(["docker", "inspect", "--format", template, identifier], "docker safe status"))
            service = data.get("service")
            if service not in APPS:
                continue
            found.add(service)
            self.report["legacy_containers"].append(data)
            self.check(service + ": legacy container stopped", not data["running"] and not data["restarting"] and not data["paused"] and data["status"] == "exited")
            if service == "zomboid":
                self.check("legacy game exited cleanly", data["exit_code"] == 0)
        self.check("all four expected legacy services accounted for", found == set(APPS))

    def retired_legacy(self):
        self.report["legacy_mode"] = "retired"
        self.report["legacy_units"] = {}
        # These are the OS Docker/containerd units, not k3s's embedded containerd.
        # systemctl show only reads manager state and cannot socket-activate Docker.
        for unit in (*LEGACY_UNITS, "k3s.service"):
            properties = systemd_unit(unit)
            self.report["legacy_units"][unit] = properties
            loaded = properties["LoadState"] == "loaded"
            if unit == "k3s.service":
                self.check("k3s.service: active and running", loaded
                           and properties["ActiveState"] == "active" and properties["SubState"] == "running")
            else:
                self.check(unit + ": inactive and disabled", loaded
                           and properties["ActiveState"] == "inactive" and properties["SubState"] == "dead"
                           and properties["UnitFileState"] == "disabled")
        path = DOCKER_CONTAINERS
        self.report["legacy_container_directory"] = {"path": str(path)}
        # Reject symlinks in the path as well as a symlink at its final component,
        # including dangling links that otherwise look like an absent directory.
        if not self.check("legacy container directory: no symlink traversal", path.resolve() == path):
            return
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            self.report["legacy_container_directory"]["present"] = False
            self.check("legacy container directory: empty or absent", True)
            return
        self.report["legacy_container_directory"]["present"] = True
        if not self.check("legacy container directory: regular directory", stat.S_ISDIR(metadata.st_mode)):
            return
        with os.scandir(path) as entries:
            empty = next(entries, None) is None
        self.report["legacy_container_directory"]["empty"] = empty
        self.check("legacy container directory: empty or absent", empty)

    def secrets(self):
        self.report["secrets"] = {}
        for namespace, expected in SECRETS.items():
            # kubectl renders names only; Secret data never enters this script.
            names = [line.removeprefix("secret/") for line in kubectl("get", "secrets", "-n", namespace, "-o", "name").splitlines() if line]
            self.report["secrets"][namespace] = sorted(names)
            self.check(namespace + ": required Secret names present", expected <= set(names))

    def images_and_uid(self):
        self.report["images"] = []
        for name, namespace in APPS.items():
            group = app_pods(self.pods, name, namespace)
            if len(group) != 1:
                self.check(name + ": image verified on unique pod", False)
                continue
            pod = group[0]
            containers = pod.get("spec", {}).get("containers", [])
            statuses = {c["name"]: c for c in pod.get("status", {}).get("containerStatuses", [])}
            selected = [c for c in containers if c["name"] == name]
            if not self.check(name + ": expected application container exists", len(selected) == 1):
                continue
            container = selected[0]
            status = statuses.get(name, {})
            image = container.get("image", "")
            limits = container.get("resources", {}).get("limits", {})
            self.report["images"].append({"namespace": namespace, "pod": pod["metadata"]["name"], "container": name, "reference": image, "running_image_id": status.get("imageID"), "pull_policy": container.get("imagePullPolicy"), "limits": limits})
            self.check(name + ": concrete running image reference", bool(image) and "REPLACE" not in image and not image.endswith(":latest") and bool(status.get("imageID")))
            self.check(name + ": exact CPU/RAM container limits", all(quantity(limits.get(key, "0")) == quantity(value) for key, value in APP_LIMITS[name].items()))
            if name == "zomboid":
                requests = container.get("resources", {}).get("requests", {})
                self.check("game: ephemeral-storage request is 1536 MiB", quantity(requests.get("ephemeral-storage", "0")) == 1536 * MIB)
                self.check("game: ephemeral-storage limit is 3 GiB", quantity(limits.get("ephemeral-storage", "0")) == 3 * GIB)
                self.report["game_ephemeral_storage"] = {"request": requests.get("ephemeral-storage"), "limit": limits.get("ephemeral-storage")}
            if name == "panel":
                official = re.fullmatch(r"ghcr\.io/fpsacha/zomboid-panel:v?\d+\.\d+\.\d+@sha256:[0-9a-f]{64}", image)
                legacy = image == "docker.io/local/pz-panel:migration-6a953f186a357932"
                self.check("panel: stable official digest or exact migration image",
                           bool(official) and container.get("imagePullPolicy") == "IfNotPresent"
                           or legacy and container.get("imagePullPolicy") == "Never")
            elif name != "otel-collector":
                self.check(name + ": imported image uses Never pull policy", container.get("imagePullPolicy") == "Never")
        game = app_pods(self.pods, "zomboid", "zomboid")
        if not self.check("game UID can be verified on Ready pod", len(game) == 1 and pod_ready(game[0])):
            return
        pod = game[0]
        configured_uid = pod.get("spec", {}).get("securityContext", {}).get("runAsUser")
        probe = "import os,json; from pathlib import Path; print(json.dumps({'uid':os.geteuid(),'cgroup':{name:Path('/sys/fs/cgroup',name).read_text().strip() for name in ['memory.max','memory.current','cpu.max','pids.max']}}))"
        result = json.loads(kubectl("exec", "-n", "zomboid", pod["metadata"]["name"], "-c", "zomboid", "--", "python3", "-c", probe))
        actual_uid = int(result["uid"])
        self.report["game_uid"] = {"configured": configured_uid, "effective": actual_uid}
        self.check("game runs as UID 1000", configured_uid == 1000 and actual_uid == 1000)
        self.report["game_cgroup"] = result["cgroup"]
        self.check("game kernel memory limit is 10 GiB", result["cgroup"]["memory.max"] == str(10 * 1024**3))
        quota, period = result["cgroup"]["cpu.max"].split()
        self.check("game kernel CPU quota is 2200m", quota != "max" and int(quota) * 1000 == 2200 * int(period))

    def run(self):
        self.check("verification runs as root", os.geteuid() == 0)
        self.check("verification runs on expected VM", socket.gethostname() == NODE)
        if not self.report["failures"]:
            for name in ("cluster", "quotas", "claims", "filesystems", "legacy", "secrets", "images_and_uid"):
                self.section(name, getattr(self, name))
        self.report["ok"] = not self.report["failures"]
        self.report["finished_at_utc"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        print(json.dumps(self.report, ensure_ascii=False, indent=2))
        return 0 if self.report["ok"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--wait-ready-seconds", type=int, default=0, help="Wait for application and ServiceLB readiness, up to 1800 seconds; default is immediate.")
    parser.add_argument("--minimum-root-free-mib", type=int, default=2048, help="Required free root filesystem capacity; cannot be less than 2048 MiB.")
    parser.add_argument("--legacy-retired", action="store_true", help="Check disabled legacy Docker/OS containerd units and absent container data without invoking Docker CLI; default checks the four stopped migration containers.")
    options = parser.parse_args()
    if not 0 <= options.wait_ready_seconds <= 1800:
        parser.error("--wait-ready-seconds must be between 0 and 1800")
    if options.minimum_root_free_mib < 2048:
        parser.error("--minimum-root-free-mib must be at least 2048")
    return Verification(options).run()


if __name__ == "__main__":
    sys.exit(main())
