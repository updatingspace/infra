#!/usr/bin/env python3
"""Copy only completed encrypted PostgreSQL backups to the chosen workstation."""

import hashlib
from pathlib import Path
import re
import subprocess


SSH = [
    "ssh", "-F", "/dev/null", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
    "updspace_m4tveevm@192.168.1.176",
]
REMOTE_ROOT = "/srv/updspace/backups/postgres"
LOCAL_ROOT = Path.home() / ".local/share/updspace-backups/portal-postgres"
ARCHIVE_NAME = "postgres.tar.gpg"
BACKUP_NAME = re.compile(r"[0-9]{8}T[0-9]{6}Z-[A-Za-z0-9]+")


def verify_archive(path: Path, expected: str) -> None:
    match = re.fullmatch(r"([0-9a-f]{64})  " + re.escape(ARCHIVE_NAME) + r"\n", expected)
    if not match:
        raise ValueError("Invalid SHA256 manifest")
    with path.open("rb") as archive:
        actual = hashlib.file_digest(archive, "sha256").hexdigest()
    if actual != match[1]:
        raise ValueError("Archive SHA256 does not match source")


def sync() -> None:
    if not re.fullmatch(r"[a-z][a-z0-9-]*\.tar\.gpg", ARCHIVE_NAME):
        raise ValueError("Invalid archive name")
    LOCAL_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    listing = subprocess.check_output(
        SSH + ["sudo -n sh -c 'for d in " + REMOTE_ROOT + "/*; do "
               f'if test -f "$d/COMMITTED" && test -f "$d/{ARCHIVE_NAME}.sha256"; '
               'then basename "$d"; fi; done\''],
        text=True, timeout=30,
    )
    for name in listing.splitlines():
        if not BACKUP_NAME.fullmatch(name):
            raise ValueError("Unexpected remote backup name")
        target = LOCAL_ROOT / name
        if (target / "COMMITTED").exists():
            verify_archive(
                target / ARCHIVE_NAME,
                (target / (ARCHIVE_NAME + ".sha256")).read_text(),
            )
            continue
        target.mkdir(mode=0o700, exist_ok=True)
        expected = subprocess.check_output(
            SSH + [f"sudo -n cat {REMOTE_ROOT}/{name}/{ARCHIVE_NAME}.sha256"],
            text=True, timeout=30,
        )
        if not re.fullmatch(r"([0-9a-f]{64})  " + re.escape(ARCHIVE_NAME) + r"\n", expected):
            raise ValueError("Invalid remote SHA256 manifest")
        partial = target / (ARCHIVE_NAME + ".part")
        partial.touch(mode=0o600, exist_ok=True)
        with partial.open("wb") as output:
            subprocess.run(
                SSH + [f"sudo -n cat {REMOTE_ROOT}/{name}/{ARCHIVE_NAME}"],
                stdout=output, check=True, timeout=600,
            )
        verify_archive(partial, expected)
        partial.replace(target / ARCHIVE_NAME)
        (target / (ARCHIVE_NAME + ".sha256")).write_text(expected)
        (target / "COMMITTED").touch(mode=0o600)
        print(f"Verified encrypted backup: {name}")


if __name__ == "__main__":
    sync()
