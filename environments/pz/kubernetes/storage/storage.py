#!/usr/bin/env python3
"""Audited one-host, offline migration to bounded ext4 filesystems over SSH.

No filesystem is ever formatted in place. Images and migration journals remain
outside Terraform state. Source release requires two independent validations:
the verified archive and an rsync checksum/metadata comparison of the copy.
"""
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time


MIB = 1024 * 1024
BEGIN = "# BEGIN pz-k3s bounded storage (Terraform)"
END = "# END pz-k3s bounded storage (Terraform)"
JOB_UNIT = "pz-storage-migration.service"
JOB_TIMEOUT_SECONDS = 2 * 60 * 60
JOB_POLL_SECONDS = 15
MAX_SSH_FAILURES = 120
DISK_MIGRATION_JOURNAL = Path("/var/lib/pz-backup/disk-migration/journal.json")
FSTAB = Path("/etc/fstab")


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def require_legacy_storage(spec):
    # Any attempted cutover retires this one-time loop provisioner, including
    # an interrupted cutover. Never regenerate its old fstab over a new disk.
    require(not os.path.lexists(DISK_MIGRATION_JOURNAL),
            "Data disk migration exists; legacy loop provisioning is disabled")
    target = str(Path(spec["mount_root"]) / "zomboid")
    image = str(Path(spec["image_root"]) / "zomboid.ext4")
    rows = [line.split() for line in FSTAB.read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")]
    matches = [row for row in rows if len(row) > 1 and row[1] == target]
    require(len(matches) <= 1 and all(row[0] == image for row in matches),
            "Data mount no longer belongs to legacy loop provisioning")


def run(argv, **kwargs):
    return subprocess.run([str(a) for a in argv], check=True, text=True, **kwargs)


def capture(argv):
    return run(argv, stdout=subprocess.PIPE).stdout.strip()


def atomic_write(path, data, mode=0o600):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        os.fchmod(handle.fileno(), mode)
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def digest(path):
    value = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(8 * MIB), b""):
            value.update(block)
    return value.hexdigest()


def validate_spec(spec):
    require(spec["expected_host"] == "compute-vm-2-6-60-ssd-1785610759198", "Unexpected host specification")
    require(spec["source_root"] == "/opt/pz-stack/data", "Unexpected source root")
    require(spec["image_root"] == "/var/lib/pz-volumes", "Unexpected image root")
    require(spec["mount_root"] == "/srv/pz-storage", "Unexpected mount root")
    require(spec["backup_manifest"] == "/var/backups/pz-k3s-20260930/verified.json", "Unexpected backup manifest")
    require(spec["filesystems"] == {
        "zomboid": {"size_mib": 26624, "directories": ["pz-server", "zomboid", "steam", "panel", "panel-logs"]},
        "edge": {"size_mib": 256, "directories": ["caddy-data", "caddy-config"]},
        "observability": {"size_mib": 512, "directories": ["otelcol"]},
    }, "Filesystem layout differs; use a separately reviewed growth/migration flow")
    require(spec["minimum_host_free_mib"] >= 2048, "Host reserve must be at least 2 GiB")
    require(isinstance(spec["release_verified_sources"], bool), "Source release must be a boolean")


def verify_backup(spec):
    manifest = Path(spec["backup_manifest"])
    require(manifest.is_file() and not manifest.is_symlink(), "Verified backup manifest missing")
    result = json.loads(manifest.read_text())
    archive = Path("/var/backups/pz-k3s-20260930/pz-stack.tar.gz")
    require(result.get("archive") == str(archive), "Backup archive path mismatch")
    require(result.get("verified_gzip") is True and result.get("game_exit_code") == 0, "Backup was not verified after clean game shutdown")
    require(result.get("world_files", 0) > 0 and result.get("members", 0) > 0, "Backup manifest contains no world files")
    require(isinstance(result.get("verified_at"), str) and result["verified_at"], "Backup verification time is missing")
    require(archive.is_file() and not archive.is_symlink(), "Backup archive missing")
    require(archive.stat().st_size == result.get("archive_bytes"), "Backup size changed")
    require(re.fullmatch(r"[0-9a-f]{64}", result.get("sha256", "")) is not None, "Invalid archive checksum")
    print("Checking archive SHA256 before storage migration", flush=True)
    require(digest(archive) == result["sha256"], "Backup archive checksum failed")
    return result["sha256"]


def verify_docker_stopped():
    ids = capture(["docker", "ps", "-aq", "--filter", "label=com.docker.compose.project=pz-b42", "--filter", "label=com.docker.compose.oneoff=False"]).split()
    require(ids, "No Compose containers found; cannot prove offline migration")
    containers = json.loads(capture(["docker", "inspect", *ids]))
    found = set()
    for container in containers:
        service = container["Config"]["Labels"].get("com.docker.compose.service")
        if service not in {"zomboid", "panel", "caddy", "otel-collector"}:
            continue
        found.add(service)
        state = container["State"]
        require(not state["Running"] and not state.get("Restarting") and not state.get("Paused"), "Compose service is active: " + service)
        if service == "zomboid":
            require(state["Status"] == "exited" and state["ExitCode"] == 0, "Game must have stopped cleanly with exit code 0")
    require(found == {"zomboid", "panel", "caddy", "otel-collector"}, "Expected all four stopped Compose services")


def verify_kubernetes_idle():
    executable = Path("/usr/local/bin/k3s")
    if not executable.exists():
        return
    configuration = Path("/etc/rancher/k3s/operator.yaml")
    if not configuration.exists():
        configuration = Path("/etc/rancher/k3s/k3s.yaml")
    result = run([executable, "kubectl", "--kubeconfig", configuration, "get", "pods", "-A", "-o", "json", "--request-timeout=15s"], stdout=subprocess.PIPE)
    pods = json.loads(result.stdout)["items"]
    for pod in pods:
        namespace = pod["metadata"].get("namespace")
        phase = pod.get("status", {}).get("phase")
        require(namespace not in {"zomboid", "edge", "observability"} or phase in {"Succeeded", "Failed"}, "Stop Kubernetes application pods before migrating their data")


def filesystem_identity(image):
    output = capture(["blkid", "-p", "-o", "export", image])
    return dict(line.split("=", 1) for line in output.splitlines() if "=" in line)


def verify_mount(image, mount, expected_uuid):
    require(run(["mountpoint", "-q", mount], stdout=subprocess.DEVNULL).returncode == 0, "Required filesystem is not mounted")
    records = json.loads(capture(["findmnt", "--json", "--mountpoint", mount, "-o", "SOURCE,FSTYPE,UUID,TARGET"]))["filesystems"]
    require(len(records) == 1, "Ambiguous mount")
    mounted = records[0]
    require(mounted["fstype"] == "ext4" and mounted["uuid"] == expected_uuid and mounted["target"] == str(mount), "Mounted filesystem identity mismatch")
    backing = capture(["losetup", "--noheadings", "--raw", "--output", "BACK-FILE", mounted["source"]])
    require(Path(backing).resolve() == image.resolve(), "Loop mount uses a different image")


def prepare_filesystem(name, fs, spec, journal, save):
    image = Path(spec["image_root"]) / (name + ".ext4")
    mount = Path(spec["mount_root"]) / name
    size = fs["size_mib"] * MIB
    require(not image.is_symlink(), "Filesystem image must not be a symlink")
    if not image.exists():
        temporary = image.with_suffix(".ext4.creating")
        require(not temporary.exists(), "Interrupted image creation exists; inspect it before retrying")
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        try:
            os.ftruncate(descriptor, size)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        run(["mkfs.ext4", "-q", "-F", "-m", "0", "-L", "pz-" + name, temporary])
        os.rename(temporary, image)
    require(image.is_file() and image.stat().st_size == size, "Image size differs; automatic resizing/formatting is forbidden")
    identity = filesystem_identity(image)
    require(identity.get("TYPE") == "ext4" and identity.get("LABEL") == "pz-" + name and identity.get("UUID"), "Existing image is not the expected initialized ext4 filesystem")
    previous = journal["filesystems"].get(name)
    if previous:
        require(previous["uuid"] == identity["UUID"] and previous["size_mib"] == fs["size_mib"], "Existing image identity changed")
    else:
        journal["filesystems"][name] = {"uuid": identity["UUID"], "size_mib": fs["size_mib"]}
        save()
    mount.mkdir(parents=True, exist_ok=True)
    require(not mount.is_symlink(), "Mount target must not be a symlink")
    if subprocess.run(["mountpoint", "-q", str(mount)]).returncode != 0:
        require(not any(mount.iterdir()), "Unmounted mount target contains data; refusing to hide it")
        run(["mount", "-t", "ext4", "-o", "loop,nodev,nosuid,noatime", image, mount])
    verify_mount(image, mount, identity["UUID"])
    return image, mount, identity["UUID"]


def managed_fstab(original, lines):
    require(original.count(BEGIN) == original.count(END) and original.count(BEGIN) <= 1, "Malformed managed fstab block")
    block = BEGIN + "\n" + "\n".join(lines) + "\n" + END
    if BEGIN in original:
        before, rest = original.split(BEGIN, 1)
        _, after = rest.split(END, 1)
        return before + block + after
    return original.rstrip() + "\n\n" + block + "\n"


def configure_boot(spec):
    require_legacy_storage(spec)
    mounts = [str(Path(spec["mount_root"]) / name) for name in spec["filesystems"]]
    lines = [f'{spec["image_root"]}/{name}.ext4 {spec["mount_root"]}/{name} ext4 loop,nodev,nosuid,noatime 0 0' for name in spec["filesystems"]]
    fstab = Path("/etc/fstab")
    original = fstab.read_text()
    unmanaged = original.split(BEGIN, 1)[0] + (original.split(END, 1)[1] if END in original else "")
    require(not any(path in unmanaged for path in mounts), "Unmanaged fstab entries already reference these mountpoints")
    backup = Path(spec["image_root"]) / "fstab.before-migration"
    if not backup.exists():
        atomic_write(backup, original)
    updated = managed_fstab(original, lines)
    if updated != original:
        atomic_write(fstab, updated, stat.S_IMODE(fstab.stat().st_mode))
    dropin = "[Unit]\nRequiresMountsFor=" + " ".join(mounts) + "\n"
    dropin += "".join("ConditionPathIsMountPoint=" + path + "\n" for path in mounts)
    for service in ("k3s", "docker"):
        directory = Path("/etc/systemd/system") / (service + ".service.d")
        directory.mkdir(parents=True, exist_ok=True)
        atomic_write(directory / "20-pz-storage.conf", dropin, 0o644)
    run(["systemctl", "daemon-reload"])
    run(["findmnt", "--verify", "--tab-file", "/etc/fstab"])


def verify_copy(source, target):
    comparison = capture(["rsync", "--dry-run", "-aHAXc", "--numeric-ids", "--delete", "--itemize-changes", str(source) + "/", str(target) + "/"])
    require(not comparison, "Source/copy checksum or metadata differs: " + comparison[:1000])


def directory_identity(path):
    metadata = path.lstat()
    require(stat.S_ISDIR(metadata.st_mode), "Verified path must remain an ordinary directory")
    return metadata.st_dev, metadata.st_ino


def migrate_directory(name, mounted, spec, journal, save, backup_sha):
    image, mount, uuid = mounted
    verify_mount(image, mount, uuid)
    source = Path(spec["source_root"]) / name
    retired = source.with_name(name + ".pre-k3s")
    target = mount / name
    require(not target.is_symlink(), "Destination must not be a symlink")
    entry = journal["directories"].get(name)
    verified_this_run = False
    verified_source_identity = None
    verified_target_identity = None

    def compare_once(current_source):
        nonlocal verified_this_run, verified_source_identity, verified_target_identity
        source_identity = directory_identity(current_source)
        target_identity = directory_identity(target)
        if verified_this_run:
            # Only a same-invocation rename is reusable. Journal timestamps are
            # never used as proof for a resumed migration or retained source.
            require(source_identity == verified_source_identity and target_identity == verified_target_identity, "Verified directory identity changed; refusing to reuse the checksum comparison")
            return
        verify_docker_stopped()
        verify_kubernetes_idle()
        verify_copy(current_source, target)
        require(directory_identity(current_source) == source_identity and directory_identity(target) == target_identity, "Directory identity changed during checksum comparison")
        verified_source_identity = source_identity
        verified_target_identity = target_identity
        verified_this_run = True

    if source.is_symlink():
        require(source.resolve() == target and entry and entry.get("backup_sha256") == backup_sha, "Unrecognized source symlink")
        require(target.is_dir(), "Migrated target missing")
        if not retired.exists():
            require(entry["status"] in {"linked", "releasing", "released"}, "Missing retired source without a verified migration")
            entry["status"] = "released"
            save()
            return
        require(entry["status"] != "releasing", "Source release was interrupted; compare destination with the backup and review the partial retired directory before cleanup")
    else:
        if not source.exists():
            require(retired.is_dir() and not retired.is_symlink() and entry and entry["status"] == "verified", "Source missing without a recoverable verified copy")
            require(entry.get("backup_sha256") == backup_sha, "Backup changed during migration")
        else:
            require(source.is_dir() and not retired.exists(), "Source is not an original directory or retired path already exists")
            require(not os.path.ismount(source), "Source is an independent mount; review manually")
            if entry is None:
                require(not target.exists(), "Unmanaged destination already exists")
                entry = {"source": str(source), "target": str(target), "retired": str(retired), "status": "copying", "backup_sha256": backup_sha}
                journal["directories"][name] = entry
                save()
            require(entry["backup_sha256"] == backup_sha, "Backup changed during migration")
            required_bytes = int(capture(["du", "-s", "--block-size=1", "--apparent-size", source]).split()[0])
            host_free = shutil.disk_usage(spec["image_root"]).free
            target_free = shutil.disk_usage(mount).free
            require(host_free >= required_bytes + spec["minimum_host_free_mib"] * MIB, "Copy would reduce physical host free space below 2 GiB; inspect space before proceeding")
            require(target_free >= required_bytes, "Bounded filesystem cannot fit this source directory")
            target.mkdir(exist_ok=True)
            print("Copying " + name + " with ownership, ACLs and xattrs", flush=True)
            run(["rsync", "-aHAX", "--numeric-ids", "--sparse", str(source) + "/", str(target) + "/"])
            compare_once(source)
            os.sync()
            entry["status"] = "verified"
            entry["verified_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            save()
            os.rename(source, retired)
        compare_once(retired)
        os.symlink(str(target), source)
        entry["status"] = "linked"
        save()
    if spec["release_verified_sources"]:
        require(retired.is_dir() and not retired.is_symlink() and not os.path.ismount(retired), "Retired source must be an ordinary directory")
        compare_once(retired)
        verify_mount(image, mount, uuid)
        entry["status"] = "releasing"
        save()
        print("Releasing checksum-verified backed-up source: " + str(retired), flush=True)
        shutil.rmtree(retired)
        entry["status"] = "released"
        save()
    else:
        print("Retained verified source: " + str(retired), flush=True)


def apply_remote(spec):
    os.umask(0o077)
    validate_spec(spec)
    require(os.geteuid() == 0, "Root privileges required on the target host")
    require(capture(["hostname"]) == spec["expected_host"], "Refusing unexpected host")
    for executable in ["rsync", "mkfs.ext4", "blkid", "findmnt", "losetup", "mountpoint", "mount", "docker", "systemctl", "du"]:
        require(shutil.which(executable), "Install required tool first: " + executable)
    root = Path(spec["image_root"])
    require(not root.is_symlink(), "Image root cannot be a symlink")
    root.mkdir(parents=True, exist_ok=True)
    with open(root / "migration.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        require_legacy_storage(spec)
        state = root / "migration.json"
        journal = json.loads(state.read_text()) if state.exists() else {"filesystems": {}, "directories": {}}
        def save():
            atomic_write(state, json.dumps(journal, indent=2, sort_keys=True) + "\n")
        verify_docker_stopped()
        pending = any(not (Path(spec["source_root"]) / name).is_symlink() or (Path(spec["source_root"]) / (name + ".pre-k3s")).exists() for fs in spec["filesystems"].values() for name in fs["directories"])
        if pending:
            verify_kubernetes_idle()
        backup_sha = verify_backup(spec)
        mounted = {name: prepare_filesystem(name, fs, spec, journal, save) for name, fs in spec["filesystems"].items()}
        configure_boot(spec)
        for namespace, fs in spec["filesystems"].items():
            for name in fs["directories"]:
                migrate_directory(name, mounted[namespace], spec, journal, save, backup_sha)
        journal["completed_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        save()
        print("Storage migration complete; hard filesystem capacities: zomboid 26 GiB, edge 256 MiB, observability 512 MiB.", flush=True)
        print("All images share one SSD. Sparse images do not reserve host space; monitor host free space and keep backups off-host.", flush=True)


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def job_identity(spec, script):
    return {
        "unit": JOB_UNIT,
        "spec_sha256": hashlib.sha256(canonical_json(spec).encode()).hexdigest(),
        "script_sha256": hashlib.sha256(script.encode()).hexdigest(),
    }


def unit_properties():
    properties = ["LoadState", "ActiveState", "SubState", "ExecMainCode", "ExecMainStatus", "MainPID", "Result"]
    result = subprocess.run(["systemctl", "show", "--no-pager", *["--property=" + name for name in properties], JOB_UNIT], text=True, capture_output=True, timeout=15)
    values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    require(values.get("LoadState") in {"loaded", "not-found"}, "Could not determine persistent storage unit state")
    return values


def safe_progress(journal, spec):
    known_names = {name for fs in spec["filesystems"].values() for name in fs["directories"]}
    known_states = {"copying", "verified", "linked", "releasing", "released"}
    return {
        name: entry["status"]
        for name, entry in journal.get("directories", {}).items()
        if name in known_names and entry.get("status") in known_states
    }


def job_status(properties, identity, journal, result, spec):
    status = {
        "unit": JOB_UNIT,
        "job": identity["spec_sha256"],
        "state": "running",
        "directories": safe_progress(journal, spec),
        "phase": "copying-or-verifying" if journal.get("directories") else "backup-verification-or-filesystem-preparation",
    }
    active = properties.get("ActiveState")
    substate = properties.get("SubState")
    exited = active == "active" and substate == "exited"
    if exited:
        unit_success = properties.get("ExecMainCode") == "1" and properties.get("ExecMainStatus") == "0" and properties.get("Result") == "success" and properties.get("MainPID") == "0"
        proof = (
            unit_success and result.get("ok") is True and result.get("exit_code") == 0
            and all(result.get(key) == value for key, value in identity.items())
            and bool(journal.get("completed_at"))
            and result.get("completed_at") == journal.get("completed_at")
        )
        status["state"] = "succeeded" if proof else "failed"
        status["phase"] = "complete" if proof else "completion-proof-missing"
        status["exit_status"] = int(properties.get("ExecMainStatus", "-1"))
    elif active == "inactive" and properties.get("ExecMainCode") == "0" and not result:
        status["phase"] = "queued"
    elif active in {"failed", "inactive"} or properties.get("LoadState") == "not-found":
        status["state"] = "failed"
        status["phase"] = "unit-failed-or-missing"
        status["exit_status"] = int(properties.get("ExecMainStatus", "-1"))
    return status


def write_job_file(path, content):
    require(not path.is_symlink(), "Persistent job files must not be symlinks")
    if path.exists():
        require(path.is_file() and path.read_text() == content, "Existing persistent job content differs; inspect before replacing it")
        path.chmod(0o600)
    else:
        atomic_write(path, content, 0o600)


def ensure_persistent_job(payload):
    """Short SSH operation. Fixed unit name and a lock make retries idempotent."""
    os.umask(0o077)
    spec = payload["spec"]
    script = payload["script"]
    validate_spec(spec)
    require(os.geteuid() == 0, "Root privileges required for storage job control")
    require(capture(["hostname"]) == spec["expected_host"], "Refusing unexpected host")
    identity = job_identity(spec, script)
    root = Path(spec["image_root"])
    require(not root.is_symlink(), "Image root cannot be a symlink")
    root.mkdir(parents=True, exist_ok=True)
    job = root / "job"
    require(not job.is_symlink(), "Persistent job directory cannot be a symlink")
    job.mkdir(mode=0o700, exist_ok=True)
    job.chmod(0o700)
    with open(root / "job-control.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        metadata = job / "identity.json"
        properties = unit_properties()
        if not metadata.exists():
            require(properties["LoadState"] == "not-found", "An unmanaged storage migration unit already exists")
        write_job_file(job / "storage.py", script)
        write_job_file(job / "spec.json", canonical_json(spec) + "\n")
        write_job_file(metadata, canonical_json(identity) + "\n")
        result_path = job / "result.json"
        result = json.loads(result_path.read_text()) if result_path.exists() else {}
        if properties["LoadState"] == "not-found":
            # Never silently repeat a finished/failed job after a reboot or an
            # operator's reset-failed. Uncertain launch attempts without a result
            # may retry the same unit name; systemd atomically rejects duplicates.
            require(not result, "Storage job result exists but its systemd unit is missing; inspect before a new attempt")
            launched = subprocess.run([
                "systemd-run", "--unit=" + JOB_UNIT, "--service-type=exec", "--remain-after-exit", "--no-block", "--quiet",
                "--property=Restart=no", "--property=UMask=0077", "--property=KillMode=control-group",
                "--property=StandardOutput=journal", "--property=StandardError=journal",
                "/usr/bin/python3", str(job / "storage.py"), "--run-job",
            ], text=True, capture_output=True, timeout=20)
            properties = unit_properties()
            require(properties["LoadState"] == "loaded", "Persistent storage unit was not created; launch may be retried safely")
            require(launched.returncode == 0 or properties.get("ActiveState") in {"active", "activating"}, "Persistent storage unit failed to start")
        journal_path = root / "migration.json"
        journal = json.loads(journal_path.read_text()) if journal_path.exists() else {}
        # Read again because a short/no-op migration could finish during launch.
        result = json.loads(result_path.read_text()) if result_path.exists() else {}
        return job_status(properties, identity, journal, result, spec)


def run_persistent_job():
    """Systemd owns this worker; neither SSH nor Terraform is its parent."""
    job = Path(__file__).resolve().parent
    spec = json.loads((job / "spec.json").read_text())
    identity = json.loads((job / "identity.json").read_text())
    require(identity == job_identity(spec, (job / "storage.py").read_text()), "Persistent storage job identity mismatch")
    result_path = job / "result.json"
    require(not result_path.exists(), "Persistent storage job already has a result")
    try:
        apply_remote(spec)
        journal = json.loads((Path(spec["image_root"]) / "migration.json").read_text())
        require(bool(journal.get("completed_at")), "Migration finished without a completion journal")
        result = dict(identity, ok=True, exit_code=0, completed_at=journal["completed_at"])
        atomic_write(result_path, canonical_json(result) + "\n")
    except Exception as error:
        atomic_write(result_path, canonical_json(dict(identity, ok=False, exit_code=1, error_type=type(error).__name__)) + "\n")
        raise


class SSHTransportError(RuntimeError):
    pass


def request_job_status(spec, script, ssh_target, ssh_key, timeout=45):
    remote = "sudo -n python3 -c " + shlex.quote(script) + " --manage-job"
    try:
        result = subprocess.run([
            "ssh", "-F", "/dev/null", "-i", ssh_key, "-o", "BatchMode=yes", "-o", "ConnectionAttempts=1",
            "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=2", ssh_target, remote,
        ], input=json.dumps({"spec": spec, "script": script}), text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise SSHTransportError("SSH status request timed out; the persistent job may still be running") from None
    if result.returncode == 255:
        raise SSHTransportError("SSH connection interrupted; the persistent job may still be running")
    require(result.returncode == 0, "Remote storage controller failed; preserve the job and inspect root-only systemd logs")
    try:
        status = json.loads(result.stdout)
    except ValueError:
        raise SSHTransportError("Incomplete SSH status response; reconnecting to the same persistent job") from None
    if status.get("unit") == JOB_UNIT and status.get("controller_retry") is True:
        raise SSHTransportError("Remote status inspection timed out; reconnecting to the same persistent job")
    require(status.get("unit") == JOB_UNIT and status.get("job") == job_identity(spec, script)["spec_sha256"], "Unexpected persistent job status identity")
    return status


def wait_persistent_job(spec, script, ssh_target, ssh_key, timeout=JOB_TIMEOUT_SECONDS):
    deadline = time.monotonic() + timeout
    failures = 0
    last_status = None
    while time.monotonic() < deadline:
        try:
            status = request_job_status(spec, script, ssh_target, ssh_key, timeout=max(0.1, min(45, deadline - time.monotonic())))
            failures = 0
        except SSHTransportError:
            failures += 1
            require(failures <= MAX_SSH_FAILURES, "SSH retry limit reached; persistent storage job was not stopped. Reconnect with the same script/spec to inspect it")
            if failures == 1 or failures % 4 == 0:
                print("SSH unavailable; reconnecting to existing storage job (attempt " + str(failures) + ")", flush=True)
            time.sleep(min(JOB_POLL_SECONDS, max(0, deadline - time.monotonic())))
            continue
        summary = {key: status[key] for key in ("unit", "state", "phase", "directories") if key in status}
        if summary != last_status:
            print(json.dumps(summary, sort_keys=True), flush=True)
            last_status = summary
        if status.get("state") == "succeeded":
            return status
        require(status.get("state") == "running", "Persistent storage job failed or lacks completion proof; preserve images/journal and inspect " + JOB_UNIT)
        time.sleep(min(JOB_POLL_SECONDS, max(0, deadline - time.monotonic())))
    raise RuntimeError("Two-hour storage wait deadline reached; persistent job was not stopped. Reconnect with the same script/spec to inspect it")


def main():
    if sys.argv[1:] == ["--remote"]:
        apply_remote(json.load(sys.stdin))
        return
    if sys.argv[1:] == ["--run-job"]:
        run_persistent_job()
        return
    if sys.argv[1:] == ["--manage-job"]:
        try:
            status = ensure_persistent_job(json.load(sys.stdin))
        except subprocess.TimeoutExpired:
            status = {"unit": JOB_UNIT, "controller_retry": True}
        print(json.dumps(status, sort_keys=True))
        return
    spec = json.loads(os.environ["STORAGE_SPEC"])
    validate_spec(spec)
    wait_persistent_job(spec, Path(__file__).read_text(), os.environ["SSH_TARGET"], os.environ["SSH_KEY"])


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ValueError, subprocess.CalledProcessError) as error:
        raise SystemExit("Storage migration stopped: " + str(error))
