#!/usr/bin/env python3
"""Run as root ON THE SERVER while all legacy writers are stopped."""
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tarfile
import time

backup = Path("/var/backups/pz-k3s-20260930/pz-stack.tar.gz")
os.umask(0o077)
lock = backup.with_name("verification.lock").open("a")
fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
assert not backup.with_name("verified.json").exists(), "Already verified; preserve initial proof"
for name in ("zomboid", "panel", "caddy", "otel-collector"):
    status = json.loads(subprocess.check_output(["docker", "inspect", f"pz-b42-{name}-1"]))[0]["State"]
    assert status["Status"] == "exited", name
    if name == "zomboid":
        assert status["ExitCode"] == 0 and not status["OOMKilled"]

members = files = worlds = 0
last_report = time.monotonic()
with tarfile.open(backup, "r|gz") as archive:
    for entry in archive:
        members += 1
        if not entry.isfile():
            continue
        source = Path("/opt") / entry.name
        assert source.is_relative_to("/opt/pz-stack") and ".." not in source.parts
        assert source.stat().st_size == entry.size, f"Size mismatch: {entry.name}"
        with archive.extractfile(entry) as archived, source.open("rb") as original:
            assert hashlib.file_digest(archived, "sha256").digest() == hashlib.file_digest(original, "sha256").digest(), f"Hash mismatch: {entry.name}"
        files += 1
        worlds += int("/Saves/Multiplayer/" in entry.name)
        if time.monotonic() - last_report >= 30:
            print(json.dumps(dict(phase="comparing", files=files, world_files=worlds)), flush=True)
            last_report = time.monotonic()
assert worlds > 0
with backup.open("rb") as stream:
    digest = hashlib.file_digest(stream, "sha256").hexdigest()
report = dict(archive=str(backup), archive_bytes=backup.stat().st_size, sha256=digest,
              verified_gzip=True, world_files=worlds, members=members, files=files,
              game_exit_code=0, verified_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
temporary = backup.with_name("verified.json.tmp")
with temporary.open("w") as handle:
    handle.write(json.dumps(report, indent=2) + "\n")
    handle.flush()
    os.fsync(handle.fileno())
os.replace(temporary, backup.with_name("verified.json"))
print(json.dumps(report))
