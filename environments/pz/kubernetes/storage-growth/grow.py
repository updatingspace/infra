#!/usr/bin/env python3
"""One reviewed online expansion: existing Zomboid ext4 image, 26 -> 50 GiB.

Default --check is read-only. --apply runs on the VM under a detached Terraform
service. No formatting, shrink, unmount, partition edits or migration rerun.
Interrupted phases are resumed from observed image/loop/ext4 sizes, not journal.
"""
import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess

GIB = 1024 ** 3
ORIGINAL = 26 * GIB
TARGET = 50 * GIB
RESERVE = 8 * GIB
HOST = "compute-vm-2-6-60-ssd-1785610759198"
ROOT_UUID = "a4067ea2-2d37-482f-9e75-39410b69c271"
PZ_UUID = "0bd0ee81-1d96-447e-868a-405226663cf6"
IMAGE = Path("/var/lib/pz-volumes/zomboid.ext4")
MOUNT = "/srv/pz-storage/zomboid"
JOURNAL = IMAGE.with_name("growth-zomboid-50.json")
DISK_MIGRATION_JOURNAL = Path("/var/lib/pz-backup/disk-migration/journal.json")


def require(ok, reason):
    if not ok:
        raise RuntimeError(reason)


def command(arguments):
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=180)
    require(result.returncode == 0, "command_failed:" + Path(arguments[0]).name)
    return result.stdout


def mount(arguments):
    rows = json.loads(command(["findmnt", "--json", *arguments,
                               "-o", "SOURCE,TARGET,FSTYPE,UUID,OPTIONS"]))["filesystems"]
    require(len(rows) == 1, "ambiguous_mount")
    return rows[0]


def superblock(device):
    fields = dict(line.split(":", 1) for line in command(["dumpe2fs", "-h", device]).splitlines() if ":" in line)
    return {"bytes": int(fields["Block count"]) * int(fields["Block size"]),
            "uuid": fields["Filesystem UUID"].strip(),
            "label": fields["Filesystem volume name"].strip(),
            "state": fields["Filesystem state"].strip()}


def regular_image(path):
    value = path.lstat()
    require(path.resolve() == path and stat.S_ISREG(value.st_mode) and value.st_uid == 0
            and not value.st_mode & 0o077 and value.st_nlink == 1, "unsafe_image_identity")
    return value


def observe():
    require(os.geteuid() == 0 and socket.gethostname() == HOST, "wrong_host_or_not_root")
    require(not os.path.lexists(DISK_MIGRATION_JOURNAL), "data_disk_migration_disables_legacy_growth")
    root = mount(["--mountpoint", "/"])
    backing_mount = mount(["--target", str(IMAGE)])
    pz = mount(["--mountpoint", MOUNT])
    require(root == backing_mount, "image_not_on_expected_root_mount")
    require(root["source"] == "/dev/vda1" and root["fstype"] == "ext4"
            and root["uuid"] == ROOT_UUID and "rw" in root["options"].split(","), "wrong_root_mount")
    require(pz["target"] == MOUNT and pz["fstype"] == "ext4" and pz["uuid"] == PZ_UUID
            and {"rw", "nodev", "nosuid"} <= set(pz["options"].split(",")), "wrong_zomboid_mount")
    loop = pz["source"]
    require(re.fullmatch(r"/dev/loop[0-9]+", loop) is not None, "not_a_loop_device")
    devices = json.loads(command(["losetup", "--json", "--output", "NAME,BACK-FILE,OFFSET,SIZELIMIT,RO"]))["loopdevices"]
    matches = [row for row in devices if row["back-file"] == str(IMAGE)]
    require(len(matches) == 1 and matches[0]["name"] == loop and matches[0]["offset"] == 0
            and matches[0]["sizelimit"] == 0 and matches[0]["ro"] is False, "unexpected_loop_mapping")
    migration = json.loads(IMAGE.with_name("migration.json").read_text())
    require(migration.get("completed_at") and migration["filesystems"]["zomboid"] ==
            {"size_mib": 26624, "uuid": PZ_UUID}, "migration_identity_mismatch")
    image = regular_image(IMAGE)
    other_unallocated = 0
    for name, size in (("edge", GIB // 4), ("observability", GIB // 2)):
        other = regular_image(IMAGE.with_name(name + ".ext4"))
        require(other.st_size == size, "other_environment_size_changed")
        other_unallocated += max(0, size - other.st_blocks * 512)
    filesystem = superblock(loop)
    root_filesystem = superblock("/dev/vda1")
    require(filesystem["uuid"] == PZ_UUID and filesystem["label"] == "pz-zomboid"
            and filesystem["state"] == "clean", "unexpected_zomboid_superblock")
    require(root_filesystem["uuid"] == ROOT_UUID and root_filesystem["state"] == "clean", "unexpected_root_superblock")
    free = os.statvfs(IMAGE)
    return {
        "disk_bytes": int(command(["blockdev", "--getsize64", "/dev/vda"])),
        "root_partition_bytes": int(command(["blockdev", "--getsize64", "/dev/vda1"])),
        "root_filesystem_bytes": root_filesystem["bytes"],
        "root_available_bytes": free.f_bavail * free.f_frsize,
        "other_unallocated_bytes": other_unallocated,
        "image_bytes": image.st_size, "image_allocated_bytes": image.st_blocks * 512,
        "image_inode": image.st_ino, "image_device": image.st_dev,
        "loop": loop, "loop_bytes": int(command(["blockdev", "--getsize64", loop])),
        "filesystem_bytes": filesystem["bytes"], "filesystem_uuid": filesystem["uuid"],
    }


def validate(value):
    require(value["disk_bytes"] >= 100 * GIB, "cloud_disk_not_yet_100_gib")
    require(value["root_partition_bytes"] >= 99 * GIB, "root_partition_not_yet_expanded")
    require(value["root_filesystem_bytes"] >= 99 * GIB, "root_filesystem_not_yet_expanded")
    require(ORIGINAL <= value["filesystem_bytes"] <= value["loop_bytes"] <= value["image_bytes"] <= TARGET,
            "sizes_inconsistent_or_would_shrink")
    require(value["root_available_bytes"] >= RESERVE + value["other_unallocated_bytes"]
            + max(0, TARGET - value["image_allocated_bytes"]), "insufficient_host_reserve")
    return value


def complete(value):
    return (value["image_bytes"] == value["loop_bytes"] == value["filesystem_bytes"] == TARGET
            and value["image_allocated_bytes"] >= TARGET)


def record(phase, value):
    require(not JOURNAL.is_symlink(), "journal_is_symlink")
    temporary = JOURNAL.with_name(JOURNAL.name + "." + str(os.getpid()) + ".tmp")
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump({"phase": phase, "at": datetime.now(timezone.utc).isoformat(), "observed": value}, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, JOURNAL)
    fd = os.open(JOURNAL.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def grow(observe_fn=observe, run_fn=command, allocate_fn=os.posix_fallocate, record_fn=record):
    # The existing migration uses this same lock. Do not change its journal.
    lock_fd = os.open(IMAGE.with_name("migration.lock"), os.O_RDWR | os.O_NOFOLLOW)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        require(not os.path.lexists(DISK_MIGRATION_JOURNAL), "data_disk_migration_disables_legacy_growth")
        value = validate(observe_fn())
        if complete(value):
            return value
        record_fn("preflight", value)
        descriptor = os.open(IMAGE, os.O_RDWR | os.O_NOFOLLOW)
        try:
            opened = os.fstat(descriptor)
            require((opened.st_dev, opened.st_ino, opened.st_size) ==
                    (value["image_device"], value["image_inode"], value["image_bytes"]), "image_changed_before_growth")
            # Reserve the full image on the confirmed ext4 host filesystem;
            # existing contents are retained. An interrupted allocation can resume.
            allocate_fn(descriptor, 0, TARGET)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        value = validate(observe_fn())
        require(value["image_bytes"] == TARGET and value["image_allocated_bytes"] >= TARGET, "allocation_incomplete")
        record_fn("allocated", value)
        if value["loop_bytes"] < TARGET:
            run_fn(["losetup", "--set-capacity", value["loop"]])
        value = validate(observe_fn())
        require(value["loop_bytes"] == TARGET, "loop_capacity_not_updated")
        record_fn("loop_expanded", value)
        if value["filesystem_bytes"] < TARGET:
            run_fn(["resize2fs", value["loop"], "50G"])
        value = validate(observe_fn())
        require(complete(value), "filesystem_growth_unconfirmed")
        record_fn("complete", value)
        return value
    finally:
        os.close(lock_fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args()
    require(not (arguments.apply and arguments.check), "select_check_or_apply")
    value = grow() if arguments.apply else validate(observe())
    print(json.dumps({"ok": True, "complete": complete(value), "observed": value}, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
        reason = str(error) if isinstance(error, RuntimeError) else type(error).__name__
        print(json.dumps({"ok": False, "reason": reason}))
        raise SystemExit(1) from None
