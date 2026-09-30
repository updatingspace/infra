#!/usr/bin/env python3
"""Restore only the five pre-migration startup ZIPs, preserving the current set.

Run as root on the VM under a persistent operator-owned job after review.
Without --activate, only creates and verifies staging; it never changes startup.
Only exact backup_1.zip ... backup_5.zip regular-file members are streamed to
preselected paths; no extractall is used. No world files are restored or deleted.
"""
import argparse
import datetime
import gzip
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tarfile
import zipfile

HOST = "compute-vm-2-6-60-ssd-1785610759198"
ARCHIVE = Path("/var/backups/pz-k3s-20260930/pz-stack.tar.gz")
MANIFEST = ARCHIVE.with_name("verified.json")
# Bind this recovery to the archive already verified before Kubernetes startup.
TRUSTED_SHA256 = "745155b0d93e74b42152dad168f44fa302129a41cfb00b3b72a31e92cd187f0d"
PREFIX = "pz-stack/data/zomboid/backups/startup"
NAMES = {f"backup_{number}.zip" for number in range(1, 6)}
MOUNT = Path("/srv/pz-storage/zomboid")
CURRENT = MOUNT / "zomboid/backups/startup"
AUDITS = Path("/var/lib/pz-backup-recovery")
RESERVE = 512 * 1024**2
BUFFER = 4 * 1024**2


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def read_command(args):
    result = subprocess.run(args, text=True, capture_output=True, timeout=25)
    require(result.returncode == 0, "Read-only guard command failed; diagnostics remain private")
    return result.stdout


def stopped():
    base = ["/usr/local/bin/k3s", "kubectl", "--kubeconfig", "/etc/rancher/k3s/operator.yaml", "--request-timeout=15s", "-n", "zomboid"]
    sts = json.loads(read_command(base + ["get", "statefulset", "zomboid", "-o", "json"]))
    require(sts.get("spec", {}).get("replicas") == 0, "Game StatefulSet must have replicas=0")
    pods = json.loads(read_command(base + ["get", "pods", "-o", "json"]))
    require(not any(
        pod.get("metadata", {}).get("name", "").startswith("zomboid-")
        or any(owner.get("kind") == "StatefulSet" and owner.get("name") == "zomboid" for owner in pod.get("metadata", {}).get("ownerReferences", []))
        for pod in pods.get("items", [])
    ), "Game pod still exists, including Terminating; wait for its normal exit")
    require(read_command(["docker", "inspect", "--format", "{{.State.Running}}", "pz-b42-zomboid-1"]).strip() == "false", "Legacy Docker game must remain stopped")


def fingerprint(path):
    value = path.stat()
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)


def current_metadata():
    require(CURRENT.is_dir() and not CURRENT.is_symlink(), "Startup must be a real directory")
    result = {}
    for path in CURRENT.iterdir():
        require(path.name in NAMES and path.is_file() and not path.is_symlink(), "Unexpected current startup entry; inspect before recovery")
        result[path.name] = fingerprint(path)
    require(set(result) == NAMES, "Current startup file names differ from the reviewed five-file set")
    return result


def safe_root_file(path):
    value = path.lstat()
    require(stat.S_ISREG(value.st_mode) and value.st_uid == 0 and not value.st_mode & 0o022,
            "Archive and verification manifest must be root-owned regular files without group/world write")


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def save_audit(path, report):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as output:
        json.dump(report, output, sort_keys=True, indent=2)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    temporary.chmod(0o600)
    os.replace(temporary, path)
    sync_directory(path.parent)


class HashedReader:
    def __init__(self, source):
        self.source = source
        self.sha = hashlib.sha256()

    def read(self, size=-1):
        data = self.source.read(size)
        self.sha.update(data)
        return data


def staged_file_check(path, expected_size, expected_sha):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(BUFFER), b""):
            digest.update(block)
    require(path.stat().st_size == expected_size and digest.hexdigest() == expected_sha, "Staged ZIP readback checksum mismatch")
    require(zipfile.is_zipfile(path), "Staged member is not a ZIP file")
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()  # Central directory validation; no unpacking.
        require(bool(entries), "Staged ZIP is empty")
        require(not any(entry.flag_bits & 1 for entry in entries), "Unexpected encrypted startup ZIP")
        return len(entries)


def extract_stage(stage):
    files = {}
    directory_metadata = None
    with ARCHIVE.open("rb") as raw:
        hashed = HashedReader(raw)
        with gzip.GzipFile(fileobj=hashed, mode="rb") as compressed:
            with tarfile.open(fileobj=compressed, mode="r|") as archive:
                for member in archive:
                    name = member.name.removeprefix("./").rstrip("/")
                    if name == PREFIX:
                        require(member.isdir() and directory_metadata is None, "Unexpected or duplicate startup directory member")
                        # PZ's observed startup directory is 1000:1000, mode
                        # 0775. Preserve its group write bit from the trusted
                        # archive; still refuse special bits and world write.
                        require(member.uid == 1000 and member.gid == 1000 and not member.mode & 0o7002,
                                "Unexpected startup directory ownership, special bits or world-write permission")
                        directory_metadata = member
                        continue
                    if not name.startswith(PREFIX + "/"):
                        continue
                    filename = name[len(PREFIX) + 1:]
                    require(filename in NAMES and filename not in files and member.isfile() and not member.linkname,
                            "Unexpected archive startup path, type or duplicate")
                    require(member.size > 0 and member.uid == 1000 and member.gid == 1000 and not member.mode & 0o7022,
                            "Unexpected startup ZIP size, ownership or permissions")
                    available = os.statvfs(stage)
                    require(available.f_bavail * available.f_frsize >= member.size + RESERVE,
                            "Bounded filesystem lacks room for this ZIP plus 512 MiB reserve; staging retained")
                    digest = hashlib.sha256()
                    written = 0
                    target = stage / filename
                    with archive.extractfile(member) as source, target.open("xb") as output:
                        for block in iter(lambda: source.read(BUFFER), b""):
                            output.write(block)
                            digest.update(block)
                            written += len(block)
                        output.flush()
                        os.fsync(output.fileno())
                    require(written == member.size, "Truncated startup ZIP in tar stream")
                    entries = staged_file_check(target, member.size, digest.hexdigest())
                    os.chown(target, member.uid, member.gid)
                    target.chmod(member.mode & 0o777)
                    os.utime(target, (member.mtime, member.mtime))
                    files[filename] = {"bytes": member.size, "sha256": digest.hexdigest(), "zip_entries": entries}
                    print(json.dumps({"phase": "staged-and-verified", "file": filename, "bytes": member.size}), flush=True)
            # Finish gzip CRC/trailer verification and hash every archive byte.
            for _ in iter(lambda: compressed.read(BUFFER), b""):
                pass
        for _ in iter(lambda: hashed.read(BUFFER), b""):
            pass
        require(hashed.sha.hexdigest() == TRUSTED_SHA256, "Archive SHA256 differs from trusted pre-migration backup; staging will not be activated")
    require(set(files) == NAMES and directory_metadata is not None, "Archive must contain exactly backup_1.zip through backup_5.zip and their directory")
    sync_directory(stage)
    return files, directory_metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--activate", action="store_true", help="After complete verification, preserve current directory and activate restored history")
    args = parser.parse_args()
    os.umask(0o077)
    require(os.geteuid() == 0 and read_command(["hostname"]).strip() == HOST, "Run as root on the expected VM")
    require(os.path.ismount(MOUNT) and CURRENT.resolve() == CURRENT, "Expected bounded filesystem mount or startup path is missing")
    for path in (ARCHIVE, MANIFEST):
        safe_root_file(path)
    manifest = json.loads(MANIFEST.read_text())
    require(manifest.get("sha256") == TRUSTED_SHA256 and manifest.get("archive") == str(ARCHIVE)
            and manifest.get("archive_bytes") == ARCHIVE.stat().st_size and manifest.get("verified_gzip") is True
            and manifest.get("game_exit_code") == 0 and manifest.get("world_files", 0) > 0,
            "Verified backup manifest does not match the trusted migration archive")
    stopped()
    before = current_metadata()
    before_directory = fingerprint(CURRENT)
    source_fingerprint = fingerprint(ARCHIVE)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + str(os.getpid())
    stage = CURRENT.with_name("startup.recovery-stage-" + stamp)
    preserved = CURRENT.with_name("startup.before-recovery-" + stamp)
    require(not stage.exists() and not preserved.exists(), "Recovery paths already exist")
    require(not AUDITS.is_symlink(), "Recovery audit directory cannot be a symlink")
    AUDITS.mkdir(mode=0o700, parents=True, exist_ok=True)
    require(AUDITS.stat().st_uid == 0, "Recovery audit directory must be root-owned")
    AUDITS.chmod(0o700)
    audit = AUDITS / (stamp + ".json")
    stage.mkdir(mode=0o700)
    report = {"phase": "staging", "trusted_archive_sha256": TRUSTED_SHA256, "stage": str(stage), "preserved": str(preserved)}
    save_audit(audit, report)
    files, directory = extract_stage(stage)
    require(fingerprint(ARCHIVE) == source_fingerprint, "Source archive changed while reading")
    report.update(phase="verified", files=files)
    save_audit(audit, report)
    if args.activate:
        stopped()
        require(current_metadata() == before and fingerprint(CURRENT) == before_directory, "Current startup history changed during staging; activation refused")
        require(stage.stat().st_dev == CURRENT.stat().st_dev, "Atomic directory rename requires the same filesystem")
        os.chown(stage, directory.uid, directory.gid)
        stage.chmod(directory.mode & 0o777)
        os.utime(stage, (directory.mtime, directory.mtime))
        report["phase"] = "activating"
        save_audit(audit, report)
        os.rename(CURRENT, preserved)
        try:
            os.rename(stage, CURRENT)
        except OSError:
            os.rename(preserved, CURRENT)
            raise
        sync_directory(CURRENT.parent)
        report["phase"] = "complete"
        save_audit(audit, report)
    print(json.dumps({"phase": report["phase"], "file_count": len(files), "audit": str(audit), "startup_modified": args.activate}), flush=True)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ValueError, tarfile.TarError, zipfile.BadZipFile) as error:
        if isinstance(error, RuntimeError):
            raise SystemExit(str(error)) from None
        raise SystemExit("Startup history recovery stopped; inspect the private audit/staging. No cleanup or world restore was performed.") from None
