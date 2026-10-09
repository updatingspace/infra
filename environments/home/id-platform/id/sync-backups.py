#!/usr/bin/env python3
"""Copy only completed encrypted ID backups to the chosen workstation."""

import hashlib
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess


SSH = [
    "ssh", "-F", "/dev/null", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
    "updspace_m4tveevm@192.168.1.176",
]
os.umask(0o077)

REMOTE_ROOT = "/srv/backups/updspace-id-encrypted"
LOCAL_ROOT = Path.home() / ".local/share/updspace-backups/id"
BACKUP_NAME = re.compile(r"[0-9]{8}T[0-9]{6}Z")


def verify_archive(path: Path, expected: str) -> None:
    match = re.fullmatch(r"([0-9a-f]{64})\n", expected)
    if not match:
        raise ValueError("Invalid SHA256 manifest")
    with path.open("rb") as archive:
        actual = hashlib.file_digest(archive, "sha256").hexdigest()
    if actual != match[1]:
        raise ValueError("Archive SHA256 does not match source")


def sync() -> None:
    LOCAL_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = (LOCAL_ROOT / 'sync.lock').open('w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return
    listing = subprocess.check_output(
        SSH + ["sh -c 'for d in " + REMOTE_ROOT + "/*; do "
               'if test -f "$d/COMMITTED"; '
               'then basename "$d"; fi; done\''],
        text=True, timeout=30,
    )
    for name in listing.splitlines():
        if not BACKUP_NAME.fullmatch(name):
            raise ValueError("Unexpected remote backup name")
        target = LOCAL_ROOT / name
        if (target / "COMMITTED").exists():
            verify_archive(
                target / "id.tar.gpg",
                (target / "COMMITTED").read_text(),
            )
            continue
        target.mkdir(mode=0o700, exist_ok=True)
        expected = subprocess.check_output(
            SSH + [f"cat {REMOTE_ROOT}/{name}/COMMITTED"],
            text=True, timeout=30,
        )
        if not re.fullmatch(r"([0-9a-f]{64})\n", expected):
            raise ValueError("Invalid remote SHA256 manifest")
        manifest = subprocess.check_output(
            SSH + [f"cat {REMOTE_ROOT}/{name}/manifest.json"], timeout=30,
        )
        info = json.loads(manifest)
        assert info['sha256'] == expected.strip(), 'Manifest digest mismatch'
        if shutil.disk_usage(LOCAL_ROOT).free < info['encrypted_bytes'] + 256 * 1024 * 1024:
            raise RuntimeError('Insufficient workstation space for the next ID backup')
        partial = target / "id.tar.gpg.part"
        partial.touch(mode=0o600, exist_ok=True)
        with partial.open("wb") as output:
            subprocess.run(
                SSH + [f"cat {REMOTE_ROOT}/{name}/id.tar.gpg"],
                stdout=output, check=True, timeout=600,
            )
        verify_archive(partial, expected)
        partial.replace(target / "id.tar.gpg")
        (target / "manifest.json").write_bytes(manifest)
        (target / "COMMITTED").write_text(expected)
        print(f"Verified encrypted ID backup: {name}")


if __name__ == "__main__":
    sync()
