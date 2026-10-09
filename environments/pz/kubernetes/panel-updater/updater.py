#!/usr/bin/env python3
"""Release-only panel updater. No Docker, exec, Secret API, or game mutations.

--check-only reads release/registry/Kubernetes metadata without writing anything.
--once (default) gates traffic until a single replacement panel is Ready and its
health version matches. Incomplete/ambiguous transactions require an operator.
Backups are retained; the size/free-space bounds stop updates before exhaustion.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import signal
import ssl
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


REPOSITORY = "fpsacha/zomboid-control-panel"
IMAGE = "ghcr.io/fpsacha/zomboid-panel"
SLOT = "pz.updspace.com/release-slot"
APP_SELECTOR = "app.kubernetes.io/name=panel"
DATA = Path("/panel-data")
MANAGED_NAME = ".k8s-panel-updater"
MAX_DATA = 1024 ** 3
MAX_MANAGED = 3 * 1024 ** 3
MIN_FREE = 512 * 1024 ** 2
MAX_ENTRIES = 20000
SEMVER = r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
TERMINAL = {"committed", "rolled_back"}


class Refused(Exception):
    """A fixed safe reason, never a response body or raw exception string."""


class Ambiguous(Refused):
    """A mutation may have completed: no automatic destructive recovery."""


class Deadline(Refused):
    pass


def deadline_signal(_number, _frame):
    raise Deadline("deadline_reached")


def interrupted_signal(_number, _frame):
    raise Ambiguous("updater_interrupted")


def require(condition, reason):
    if not condition:
        raise Refused(reason)


def version(value):
    require(isinstance(value, str) and re.fullmatch(SEMVER, value), "invalid_stable_version")
    return tuple(map(int, value.split(".")))


def utc():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def emit(status, phase, candidate=None):
    result = {"status": status, "phase": phase}
    if candidate:
        result.update(version=candidate["version"], digest=candidate["digest"])
    print(json.dumps(result, sort_keys=True), flush=True)


class Budget:
    def __init__(self, seconds):
        self.deadline = time.monotonic() + seconds

    def remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise Deadline("deadline_reached")
        return remaining

    def sleep(self):
        time.sleep(min(3, self.remaining()))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http_json(url, *, headers=None, body=None, method="GET", context=None,
              budget=None, limit=4 * 1024 ** 2, blob_redirect=False):
    """Never follow credential-bearing redirects, print bodies, or use proxies."""
    current_headers = dict(headers or {})
    raw_body = None if body is None else json.dumps(body, separators=(",", ":")).encode()
    for _ in range(4):
        timeout = min(20, budget.remaining()) if budget else 20
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
                                            urllib.request.HTTPSHandler(context=context))
        request = urllib.request.Request(url, data=raw_body, headers=current_headers, method=method)
        try:
            with opener.open(request, timeout=timeout) as response:
                raw = response.read(limit + 1)
                response_headers = dict(response.headers)
        except urllib.error.HTTPError as error:
            if blob_redirect and error.code in {301, 302, 303, 307, 308}:
                redirected = urllib.parse.urljoin(url, error.headers.get("Location", ""))
                parsed = urllib.parse.urlsplit(redirected)
                require(parsed.scheme == "https" and not parsed.username and not parsed.password
                        and (parsed.hostname == "ghcr.io" or (parsed.hostname or "").endswith(".githubusercontent.com")),
                        "registry_redirect_rejected")
                if parsed.netloc != urllib.parse.urlsplit(url).netloc:
                    current_headers.pop("Authorization", None)
                url = redirected
                continue
            if method != "GET" and (error.code >= 500 or error.code in {408, 429}):
                raise Ambiguous("kubernetes_mutation_response_uncertain") from None
            raise Refused("http_" + str(error.code)) from None
        except (OSError, urllib.error.URLError, http.client.HTTPException, TimeoutError, Deadline):
            if method != "GET":
                raise Ambiguous("kubernetes_mutation_transport_uncertain") from None
            raise Refused("http_transport_failed") from None
        if len(raw) > limit:
            if method != "GET":
                raise Ambiguous("kubernetes_mutation_response_too_large")
            raise Refused("http_response_too_large")
        try:
            document = json.loads(raw)
        except (ValueError, UnicodeError):
            if method != "GET":
                raise Ambiguous("kubernetes_mutation_response_invalid") from None
            raise Refused("http_response_invalid_json") from None
        return document, raw, response_headers
    raise Refused("registry_redirect_limit")


def discover(budget):
    def github(path):
        return http_json("https://api.github.com/repos/" + REPOSITORY + path,
                         headers={"Accept": "application/vnd.github+json", "User-Agent": "pz-panel-k8s-updater"},
                         budget=budget)[0]
    release = github("/releases/latest")
    require(not release.get("draft") and not release.get("prerelease"), "nonstable_release_rejected")
    tag = release.get("tag_name", "")
    require(re.fullmatch("v" + SEMVER, tag), "release_tag_rejected")
    target_version = tag[1:]
    reference = github("/git/ref/tags/" + tag).get("object", {})
    for _ in range(4):
        require(re.fullmatch(r"[0-9a-f]{40}", reference.get("sha", "")), "release_commit_invalid")
        if reference.get("type") == "commit":
            break
        require(reference.get("type") == "tag", "release_reference_invalid")
        reference = github("/git/tags/" + reference["sha"]).get("object", {})
    require(reference.get("type") == "commit", "release_tag_depth_exceeded")
    revision = reference["sha"]
    token = http_json("https://ghcr.io/token?service=ghcr.io&scope=repository:fpsacha/zomboid-panel:pull", budget=budget)[0]
    require(isinstance(token.get("token"), str), "registry_anonymous_token_missing")
    headers = {"Authorization": "Bearer " + token["token"],
               "Accept": "application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json"}

    def registry(path, expected=None, redirect=False):
        document, raw, _ = http_json("https://ghcr.io/v2/fpsacha/zomboid-panel/" + path,
                                    headers=headers, budget=budget, blob_redirect=redirect)
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        require(expected is None or digest == expected, "registry_content_digest_mismatch")
        return document, digest

    index, digest = registry("manifests/" + target_version)
    manifests = [row for row in index.get("manifests", [])
                 if row.get("platform", {}).get("architecture") == "amd64"
                 and row.get("platform", {}).get("os") == "linux"]
    require(len(manifests) == 1 and DIGEST.fullmatch(manifests[0].get("digest", "")), "amd64_manifest_missing_or_ambiguous")
    manifest, _ = registry("manifests/" + manifests[0]["digest"], manifests[0]["digest"])
    config_digest = manifest.get("config", {}).get("digest", "")
    require(DIGEST.fullmatch(config_digest), "registry_config_digest_invalid")
    config, _ = registry("blobs/" + config_digest, config_digest, True)
    labels = config.get("config", {}).get("Labels", {})
    require(config.get("architecture") == "amd64" and config.get("os") == "linux", "registry_architecture_mismatch")
    require(labels.get("org.opencontainers.image.version") == target_version
            and labels.get("org.opencontainers.image.revision") == revision
            and labels.get("org.opencontainers.image.source") == "https://github.com/" + REPOSITORY,
            "registry_release_provenance_mismatch")
    return {"version": target_version, "digest": digest, "revision": revision,
            "image": IMAGE + ":" + target_version + "@" + digest, "slot": "r-" + digest.split(":")[1][:32]}


class Kubernetes:
    def __init__(self, budget):
        service_account = Path("/var/run/secrets/kubernetes.io/serviceaccount")
        self.namespace = os.environ.get("POD_NAMESPACE", "zomboid")
        require(self.namespace == "zomboid", "unexpected_namespace")
        host = os.environ.get("KUBERNETES_SERVICE_HOST", "")
        require(bool(host), "kubernetes_service_host_missing")
        ipaddress.ip_address(host)
        port = os.environ.get("KUBERNETES_SERVICE_PORT_HTTPS", os.environ.get("KUBERNETES_SERVICE_PORT", "443"))
        require(port.isdigit() and 0 < int(port) < 65536, "kubernetes_service_port_invalid")
        self.base = "https://" + ("[" + host + "]" if ":" in host else host) + ":" + port
        self.token_file = service_account / "token"
        self.context = ssl.create_default_context(cafile=str(service_account / "ca.crt"))
        self.budget = budget

    def request(self, path, body=None):
        method = "GET" if body is None else "PATCH"
        headers = {"Authorization": "Bearer " + self.token_file.read_text().strip()}
        if body is not None:
            headers["Content-Type"] = "application/json-patch+json"
        return http_json(self.base + path, headers=headers, body=body, method=method,
                         context=self.context, budget=self.budget)[0]

    def deployment(self):
        return self.request("/apis/apps/v1/namespaces/zomboid/deployments/panel")

    def service(self):
        return self.request("/api/v1/namespaces/zomboid/services/panel")

    def pods(self):
        query = urllib.parse.urlencode({"labelSelector": APP_SELECTOR})
        return self.request("/api/v1/namespaces/zomboid/pods?" + query)["items"]

    def replicasets(self):
        query = urllib.parse.urlencode({"labelSelector": APP_SELECTOR})
        return self.request("/apis/apps/v1/namespaces/zomboid/replicasets?" + query)["items"]

    def patch(self, kind, original, operations):
        prefix = "/apis/apps/v1" if kind == "deployments" else "/api/v1"
        tests = [{"op": "test", "path": "/metadata/resourceVersion", "value": original["metadata"]["resourceVersion"]},
                 {"op": "test", "path": "/metadata/uid", "value": original["metadata"]["uid"]}]
        try:
            return self.request(prefix + "/namespaces/zomboid/" + kind + "/panel", tests + operations)
        except Deadline:
            raise Ambiguous("kubernetes_mutation_deadline_uncertain") from None
        except Refused:
            raise
        except Exception:
            raise Ambiguous("kubernetes_mutation_result_uncertain") from None


def deployment_state(document):
    spec = document["spec"]
    containers = spec["template"]["spec"]["containers"]
    require(len(containers) == 1 and containers[0]["name"] == "panel", "unexpected_panel_containers")
    require(spec.get("strategy", {}).get("type") == "Recreate", "panel_strategy_must_be_recreate")
    require(spec.get("selector", {}).get("matchLabels", {}).get("app.kubernetes.io/name") == "panel", "panel_selector_invalid")
    require(SLOT not in spec.get("selector", {}).get("matchLabels", {}), "release_slot_must_not_be_immutable_selector")
    slot = spec["template"]["metadata"].get("labels", {}).get(SLOT)
    require(isinstance(slot, str) and slot, "panel_release_slot_missing")
    return {"uid": document["metadata"]["uid"], "image": containers[0]["image"],
            "policy": containers[0].get("imagePullPolicy", "IfNotPresent"), "slot": slot,
            "replicas": spec.get("replicas", 1)}


def current_version(image, initial):
    if image.startswith(IMAGE + ":"):
        match = re.fullmatch(re.escape(IMAGE) + ":v?(" + SEMVER + r")@sha256:[0-9a-f]{64}", image)
        require(match is not None, "current_official_image_must_be_release_digest")
        return match[1]
    require("local/" in image and image.rsplit(":", 1)[-1].startswith("migration-"), "unrecognized_initial_image")
    version(initial)
    return initial


def ready(pod):
    status = pod.get("status", {})
    return (not pod.get("metadata", {}).get("deletionTimestamp") and status.get("phase") == "Running"
            and any(c.get("type") == "Ready" and c.get("status") == "True" for c in status.get("conditions", []))
            and len(status.get("containerStatuses", [])) == 1 and status["containerStatuses"][0].get("ready") is True)


def owned_pods(api, deployment_uid):
    sets = {row["metadata"]["uid"] for row in api.replicasets()
            if any(owner.get("kind") == "Deployment" and owner.get("uid") == deployment_uid
                   and owner.get("controller") is True for owner in row["metadata"].get("ownerReferences", []))}
    pods = api.pods()
    for pod in pods:
        require(any(owner.get("kind") == "ReplicaSet" and owner.get("uid") in sets
                    and owner.get("controller") is True for owner in pod["metadata"].get("ownerReferences", [])),
                "foreign_panel_labeled_pod")
    return pods


def assert_owned_state(api, expected):
    document = api.deployment()
    require(deployment_state(document) == expected, "deployment_changed_concurrently")
    return document


def wait_absent(api, expected):
    require(expected["replicas"] == 0, "cannot_wait_for_writers_without_scale_zero")
    while True:
        assert_owned_state(api, expected)
        if not owned_pods(api, expected["uid"]):
            return
        api.budget.sleep()


def wait_ready(api, expected, expected_version):
    while True:
        document = assert_owned_state(api, expected)
        pods = owned_pods(api, expected["uid"])
        status = document.get("status", {})
        if (len(pods) == 1 and ready(pods[0])
                and status.get("observedGeneration", 0) >= document["metadata"]["generation"]
                and status.get("updatedReplicas") == 1 and status.get("readyReplicas") == 1
                and status.get("replicas") == 1):
            pod = pods[0]
            container = pod["spec"]["containers"]
            require(len(container) == 1 and container[0]["name"] == "panel"
                    and container[0]["image"] == expected["image"]
                    and pod["metadata"].get("labels", {}).get(SLOT) == expected["slot"],
                    "ready_pod_does_not_match_candidate")
            address = pod.get("status", {}).get("podIP", "")
            require(ipaddress.ip_address(address) in ipaddress.ip_network("10.42.0.0/16"), "unexpected_panel_pod_ip")
            health = http_json("http://" + address + ":3001/api/health", budget=api.budget, limit=65536)[0]
            require(health.get("status") == "ok" and health.get("version") == expected_version,
                    "panel_health_version_mismatch")
            if os.environ.get("PANEL_TELEMETRY_REQUIRED") == "true":
                telemetry = health.get("telemetry", {})
                require(isinstance(telemetry, dict)
                        and telemetry.get("adapter") == "kubernetes-game-v1"
                        and telemetry.get("compatibility") == "verified"
                        and telemetry.get("scope") == "game-container-v1",
                        "panel_telemetry_compatibility_missing")
            again = owned_pods(api, expected["uid"])
            require(len(again) == 1 and again[0]["metadata"]["uid"] == pod["metadata"]["uid"]
                    and ready(again[0]) and again[0]["status"]["containerStatuses"] == pod["status"]["containerStatuses"],
                    "panel_changed_during_health_check")
            return
        api.budget.sleep()


def fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_json(path, document):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as output:
        json.dump(document, output, sort_keys=True)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
    fsync_directory(path.parent)


def read_json(path, default=None):
    if not path.exists():
        require(not path.is_symlink(), "managed_file_is_symlink")
        return default
    require(stat.S_ISREG(path.lstat().st_mode) and path.stat().st_size <= 8 * 1024 ** 2, "managed_file_invalid")
    with path.open() as source:
        return json.load(source)


@contextmanager
def locked(data):
    require(data.is_dir() and not data.is_symlink() and data.stat().st_uid == os.geteuid(), "panel_data_root_invalid")
    managed = data / MANAGED_NAME
    managed.mkdir(mode=0o700, exist_ok=True)
    require(not managed.is_symlink() and managed.stat().st_uid == os.geteuid(), "managed_directory_invalid")
    os.chmod(managed, 0o700)
    descriptor = os.open(managed / "lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refused("another_updater_holds_lock") from None
        yield managed
    finally:
        os.close(descriptor)


def walk_error(_error):
    raise Refused("filesystem_walk_failed")


def inventory(data, budget):
    records, total = [], 0
    for directory, dirs, files in os.walk(data, followlinks=False, onerror=walk_error):
        budget.remaining()
        if Path(directory) == data:
            dirs[:] = [name for name in dirs if name != MANAGED_NAME]
            require(MANAGED_NAME not in files, "managed_directory_invalid")
        for name in sorted(dirs + files):
            path = Path(directory) / name
            info = path.lstat()
            require(stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode), "panel_data_symlink_or_special_file")
            require(info.st_uid == os.geteuid() and info.st_gid == os.getegid(), "panel_data_ownership_mismatch")
            item = {"path": str(path.relative_to(data)), "mode": stat.S_IMODE(info.st_mode),
                    "mtime_ns": info.st_mtime_ns, "type": "dir" if stat.S_ISDIR(info.st_mode) else "file"}
            if item["type"] == "file":
                require(info.st_nlink == 1, "panel_data_hardlink_rejected")
                item["size"] = info.st_size
                total += info.st_size
            records.append(item)
            require(total <= MAX_DATA and len(records) <= MAX_ENTRIES, "panel_data_backup_bound_exceeded")
    return records, total


def managed_bytes(managed, budget):
    total = 0
    for directory, dirs, files in os.walk(managed, followlinks=False, onerror=walk_error):
        budget.remaining()
        for path in [Path(directory), *(Path(directory) / name for name in files)]:
            info = path.lstat()
            require(stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode), "managed_storage_special_file")
            total += max(info.st_size, info.st_blocks * 512)
        require(not any((Path(directory) / name).is_symlink() for name in dirs), "managed_storage_symlink")
        require(total <= MAX_MANAGED, "managed_backup_budget_exceeded")
    return total


def snapshot_estimate(records):
    # Include block rounding, directory entries, manifest and a second copy for
    # failed-new-data preservation. Retention is manual; never prune journals.
    estimate = sum((row.get("size", 0) + 4095) // 4096 * 4096 for row in records)
    estimate += (len(records) + 2) * 8192
    manifest_size = len(json.dumps(records).encode()) + len(records) * 90
    require(manifest_size <= 8 * 1024 ** 2, "backup_manifest_bound_exceeded")
    return estimate + manifest_size


def copy_records(source, destination, records, budget, verify=False):
    directories = [row for row in records if row["type"] == "dir"]
    for row in sorted(directories, key=lambda item: len(Path(item["path"]).parts)):
        (destination / row["path"]).mkdir(mode=0o700)
    for row in records:
        budget.remaining()
        if row["type"] != "file":
            continue
        src, dst = source / row["path"], destination / row["path"]
        digest, count = hashlib.sha256(), 0
        read_fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW)
        write_fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(read_fd, "rb") as input_file, os.fdopen(write_fd, "wb") as output_file:
            while True:
                budget.remaining()
                chunk = input_file.read(1024 * 1024)
                if not chunk:
                    break
                count += len(chunk)
                require(count <= row["size"], "panel_data_changed_during_copy")
                digest.update(chunk)
                output_file.write(chunk)
            require(count == row["size"], "panel_data_changed_during_copy")
            require(not verify or digest.hexdigest() == row["sha256"], "backup_content_hash_mismatch")
            row["sha256"] = digest.hexdigest()
            output_file.flush()
            os.fchmod(output_file.fileno(), row["mode"])
            os.fsync(output_file.fileno())
        os.utime(dst, ns=(row["mtime_ns"], row["mtime_ns"]))
    for row in sorted(directories, key=lambda item: len(Path(item["path"]).parts), reverse=True):
        path = destination / row["path"]
        fsync_directory(path)
        os.chmod(path, row["mode"])
        os.utime(path, ns=(row["mtime_ns"], row["mtime_ns"]))
    fsync_directory(destination)


def backup_preflight(data, managed, budget):
    records, total = inventory(data, budget)
    require(shutil.disk_usage(data).free >= MIN_FREE + total, "insufficient_backup_reserve")
    require(managed_bytes(managed, budget) + 2 * snapshot_estimate(records) + 1024 ** 2 <= MAX_MANAGED,
            "managed_backup_budget_requires_manual_retention")
    return records, total


def backup_data(data, managed, identifier, budget):
    records, total = backup_preflight(data, managed, budget)
    backups = managed / "backups"
    backups.mkdir(mode=0o700, exist_ok=True)
    require(not backups.is_symlink(), "backup_directory_invalid")
    destination = backups / identifier
    destination.mkdir(mode=0o700)
    (destination / "data").mkdir(mode=0o700)
    copy_records(data, destination / "data", records, budget)
    manifest = {"records": records, "bytes": total, "root_mode": stat.S_IMODE(data.stat().st_mode)}
    atomic_json(destination / "manifest.json", manifest)
    fsync_directory(backups)
    return destination


def validate_backup(backup, budget):
    manifest = read_json(backup / "manifest.json")
    require(isinstance(manifest, dict) and manifest.get("bytes", MAX_DATA + 1) <= MAX_DATA, "backup_manifest_invalid")
    records = manifest.get("records", [])
    require(len(records) <= MAX_ENTRIES, "backup_manifest_invalid")
    seen, total = set(), 0
    for row in records:
        budget.remaining()
        relative = Path(row["path"])
        require(not relative.is_absolute() and relative.parts and ".." not in relative.parts
                and relative.parts[0] != MANAGED_NAME and str(relative) not in seen, "backup_path_invalid")
        seen.add(str(relative))
        path = backup / "data" / relative
        require(not any(parent.is_symlink() for parent in [path, *path.parents]), "backup_symlink_rejected")
        info = path.lstat()
        if row["type"] == "dir":
            require(stat.S_ISDIR(info.st_mode), "backup_directory_missing")
        else:
            require(row["type"] == "file" and stat.S_ISREG(info.st_mode) and info.st_size == row["size"], "backup_file_invalid")
            total += info.st_size
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    budget.remaining()
                    digest.update(chunk)
            require(digest.hexdigest() == row["sha256"], "backup_content_hash_mismatch")
    require(total == manifest["bytes"], "backup_size_mismatch")
    return manifest


def restore_data(data, managed, backup, identifier, budget):
    # Verify every saved file BEFORE moving any current data. Preserve failed
    # new-version data by same-filesystem rename, never recursive deletion.
    manifest = validate_backup(backup, budget)
    require(shutil.disk_usage(data).free >= manifest["bytes"] + 16 * 1024 ** 2, "insufficient_restore_reserve")
    failed_records, _ = inventory(data, budget)
    require(managed_bytes(managed, budget) + snapshot_estimate(failed_records) + 1024 ** 2 <= MAX_MANAGED,
            "failed_data_preservation_exceeds_managed_budget")
    failed = managed / ("failed-data-" + identifier)
    failed.mkdir(mode=0o700)
    for entry in list(data.iterdir()):
        budget.remaining()
        if entry.name != MANAGED_NAME:
            os.rename(entry, failed / entry.name)
    fsync_directory(failed)
    fsync_directory(data)
    copy_records(backup / "data", data, manifest["records"], budget, verify=True)
    os.chmod(data, manifest["root_mode"])
    fsync_directory(data)


class Updater:
    def __init__(self, api, data, managed, candidate, initial, rollback_seconds):
        self.api, self.data, self.managed, self.candidate = api, data, managed, candidate
        self.initial, self.rollback_seconds = initial, rollback_seconds
        self.journal = None
        self.expected = None
        self.backup = None
        self.backup_validated = False
        self.traffic_attempted = False
        self.data_restoring = False

    def phase(self, name):
        self.journal["phase"] = name
        self.journal["updated_at"] = utc()
        atomic_json(self.managed / "journal.json", self.journal)
        emit("in_progress", name, self.candidate)

    def service_unchanged(self):
        service = self.api.service()
        require(service["metadata"]["uid"] == self.journal["service_uid"]
                and service["spec"]["selector"] == self.journal["service_selector"], "service_changed_concurrently")
        return service

    def scale(self, count):
        document = assert_owned_state(self.api, self.expected)
        result = self.api.patch("deployments", document, [{"op": "replace", "path": "/spec/replicas", "value": count}])
        self.expected = {**self.expected, "replicas": count}
        require(deployment_state(result) == self.expected, "deployment_scale_response_mismatch")

    def launch(self, state):
        document = assert_owned_state(self.api, self.expected)
        operations = [
            {"op": "replace", "path": "/spec/template/spec/containers/0/image", "value": state["image"]},
            {"op": "replace", "path": "/spec/template/spec/containers/0/imagePullPolicy", "value": state["policy"]},
            {"op": "replace", "path": "/spec/template/metadata/labels/" + SLOT.replace("/", "~1"), "value": state["slot"]},
            {"op": "replace", "path": "/spec/replicas", "value": 1},
        ]
        result = self.api.patch("deployments", document, operations)
        self.expected = {**state, "replicas": 1}
        require(deployment_state(result) == self.expected, "deployment_launch_response_mismatch")

    def fail_record(self):
        for kind, value in (("digests", self.candidate["digest"]), ("versions", self.candidate["version"])):
            path = self.managed / ("failed-" + kind + ".json")
            failed = read_json(path, [])
            if value not in failed:
                failed.append(value)
            atomic_json(path, failed)

    def rollback(self):
        self.api.budget = Budget(self.rollback_seconds)
        if signal.getsignal(signal.SIGALRM) is deadline_signal:
            signal.setitimer(signal.ITIMER_REAL, self.rollback_seconds)
        self.service_unchanged()
        self.phase("rollback_stopping")
        self.scale(0)
        wait_absent(self.api, self.expected)
        self.service_unchanged()
        if self.backup_validated:
            self.phase("restoring_data")
            self.data_restoring = True
            restore_data(self.data, self.managed, self.backup, self.journal["id"], self.api.budget)
            self.data_restoring = False
            self.phase("data_restored")
        self.phase("rollback_starting")
        self.launch(self.journal["old"])
        wait_ready(self.api, self.expected, self.journal["old_version"])
        self.service_unchanged()
        self.phase("rolled_back")

    def run(self):
        previous = read_json(self.managed / "journal.json")
        require(previous is None or previous.get("phase") in TERMINAL, "incomplete_journal_requires_operator")
        failed = read_json(self.managed / "failed-digests.json", [])
        require(self.candidate["digest"] not in failed, "candidate_digest_previously_failed")
        failed_versions = read_json(self.managed / "failed-versions.json", [])
        require(self.candidate["version"] not in failed_versions, "candidate_version_previously_failed")
        deployment, service = self.api.deployment(), self.api.service()
        old = deployment_state(deployment)
        require(old["replicas"] == 1, "panel_must_have_one_desired_replica")
        old_version = current_version(old["image"], self.initial)
        require(version(self.candidate["version"]) >= version(old_version), "downgrade_refused")
        if version(self.candidate["version"]) == version(old_version):
            emit("no_change", "already_current", self.candidate)
            return 0
        selector = service["spec"].get("selector", {})
        require(selector.get(SLOT) == old["slot"] and selector.get("app.kubernetes.io/name") == "panel", "service_release_selector_mismatch")
        backup_preflight(self.data, self.managed, self.api.budget)
        wait_ready(self.api, old, old_version)
        self.expected = old
        self.journal = {"id": uuid.uuid4().hex, "started_at": utc(), "candidate": self.candidate,
                        "old": old, "old_version": old_version, "service_uid": service["metadata"]["uid"],
                        "service_selector": selector}
        self.phase("prepared")
        try:
            self.service_unchanged()
            self.phase("stopping_old")
            self.scale(0)
            wait_absent(self.api, self.expected)
            self.service_unchanged()
            self.phase("backing_up")
            self.backup = backup_data(self.data, self.managed, self.journal["id"], self.api.budget)
            validate_backup(self.backup, self.api.budget)
            self.backup_validated = True
            self.journal["backup"] = str(self.backup.relative_to(self.managed))
            self.phase("backup_complete")
            self.phase("starting_candidate")
            target = {"uid": old["uid"], "image": self.candidate["image"], "policy": "IfNotPresent",
                      "slot": self.candidate["slot"], "replicas": 1}
            self.launch(target)
            wait_ready(self.api, self.expected, self.candidate["version"])
            service = self.service_unchanged()
            self.phase("traffic_release_requested")
            self.traffic_attempted = True
            selector = {**self.journal["service_selector"], SLOT: self.candidate["slot"]}
            result = self.api.patch("services", service, [{"op": "replace", "path": "/spec/selector", "value": selector}])
            require(result["spec"]["selector"] == selector, "service_switch_response_mismatch")
            self.phase("committed")
            return 0
        except BaseException as error:
            # A request with an uncertain outcome or a traffic switch is NEVER
            # followed by automatic data restoration. Preserve the journal.
            if isinstance(error, (KeyboardInterrupt, SystemExit, Ambiguous)) or self.traffic_attempted:
                emit("operator_required", self.journal["phase"], self.candidate)
                return 2
            try:
                self.fail_record()
                self.rollback()
            except BaseException:
                emit("operator_required", self.journal["phase"], self.candidate)
                return 2
            emit("failed_and_rolled_back", "rolled_back", self.candidate)
            return 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check-only", action="store_true")
    mode.add_argument("--once", action="store_true")
    args = parser.parse_args()
    candidate = None
    try:
        require(os.geteuid() == 1000, "updater_requires_uid_1000")
        update_seconds = int(os.environ.get("UPDATE_TIMEOUT_SECONDS", "600"))
        rollback_seconds = int(os.environ.get("ROLLBACK_TIMEOUT_SECONDS", "240"))
        require(60 <= update_seconds <= 600 and 60 <= rollback_seconds <= 240, "timeout_budget_invalid")
        initial = os.environ.get("INITIAL_VERSION", "1.3.7")
        version(initial)
        budget = Budget(update_seconds)
        signal.signal(signal.SIGALRM, deadline_signal)
        signal.signal(signal.SIGTERM, interrupted_signal)
        signal.setitimer(signal.ITIMER_REAL, update_seconds)
        api = Kubernetes(budget)
        candidate = discover(budget)
        if args.check_only:
            current = current_version(deployment_state(api.deployment())["image"], initial)
            require(version(candidate["version"]) >= version(current), "downgrade_refused")
            emit("available" if version(candidate["version"]) > version(current) else "no_change", "check_only", candidate)
            return 0
        with locked(DATA) as managed:
            return Updater(api, DATA, managed, candidate, initial, rollback_seconds).run()
    except Refused as error:
        emit("refused", str(error), candidate)
    except Exception:
        # Raw exception text can contain a token, signed URL, path or API body.
        emit("operator_required", "unexpected_failure", candidate)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
    return 2


if __name__ == "__main__":
    sys.exit(main())
