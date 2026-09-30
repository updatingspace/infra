#!/usr/bin/env python3
"""Run an offline PZ restore drill on a local Docker daemon, never production.

Use a freshly verified restore directory: the game will write only to its
restored data. This helper uses the archived image and game build with -nosteam.
Workshop mods can be exposed through new links within the restored copy only.
Every configured mod needs verified provenance and fresh loader evidence.
No retention attestation is ever generated.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import posixpath
import re
import signal
import stat
import struct
import subprocess
import sys
import tarfile
import time
import uuid
import zipfile
from typing import Any

import remote
import restore


GIB = 1024**3
LABEL = "org.pz.backup.restore-drill"
MOUNTS = {"pz-server": "/pz-server", "zomboid": "/zomboid", "steam": "/home/steam/Steam"}
WORLD_FILES = ("map_t.bin", "map_meta.bin")
CONTAINER_ID = re.compile(r"[0-9a-f]{64}\Z")

# Execute the recovered build with the image's own lifecycle/RCON implementation.
# Only the startup argv differs: -nosteam is explicit, and recorded in the report.
WRAPPER = b'''import json, os, re, runpy, signal, subprocess, time
from pathlib import Path
runtime = runpy.run_path('/usr/local/bin/pz-game', run_name='pz_drill_runtime')
name = os.environ['PZ_SERVER_NAME']
if not re.fullmatch(r'[A-Za-z0-9._-]+', name): raise SystemExit(2)
path = Path('/pz-server/ProjectZomboid64.json')
config = json.loads(path.read_text())
config['vmArgs'] = ['-Xms4g', '-Xmx8g'] + [x for x in config['vmArgs'] if not re.match(r'^-Xm[sx]', x, re.I)]
path.write_text(json.dumps(config, indent=2) + '\\n')
stopping = False
def stop(signum, frame):
    global stopping
    stopping = True
signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
with open('/zomboid/server-console-docker.log', 'ab', buffering=0) as output:
    child = subprocess.Popen(['/pz-server/start-server.sh', '-servername', name, '-nosteam',
                              '-adminpassword', os.environ['PZ_ADMIN_PASSWORD']],
                             stdin=subprocess.PIPE, stdout=output, stderr=subprocess.STDOUT,
                             start_new_session=True)
    sent = False
    while child.poll() is None:
        if stopping and not sent:
            sent = True
            try:
                runtime['rcon']('save')
                runtime['rcon']('quit')
            except Exception:
                try: child.stdin.write(b'save\\nquit\\n'); child.stdin.flush()
                except (BrokenPipeError, OSError): pass
        time.sleep(0.5)
    raise SystemExit(0 if stopping and child.returncode == 0 else (child.returncode or 3))
'''


class DrillError(RuntimeError):
    """Fixed safe-to-log failure, never engine responses or secret values."""


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise DrillError(reason)


class Docker:
    def __init__(self, context: str | None = None):
        if context is None:
            context = self._run(["docker", "context", "show"]).stdout.decode().strip()
        require(bool(re.fullmatch(r"[A-Za-z0-9_.-]+", context)), "invalid_docker_context")
        host = json.loads(self._run(["docker", "context", "inspect", context,
                                    "--format", "{{json .Endpoints.docker.Host}}"]).stdout)
        require(isinstance(host, str) and host.startswith("unix:///"), "local_unix_docker_context_required")
        self.base = ["docker", "--context", context]

    @staticmethod
    def _run(args: list[str], *, timeout: int = 120, check: bool = True) -> subprocess.CompletedProcess:
        result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=timeout)
        require(len(result.stdout) <= 8 * 1024 * 1024, "docker_output_limit_exceeded")
        require(not check or result.returncode == 0, "docker_command_failed")
        return result

    def run(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        return self._run([*self.base, *args], **kwargs)

    def inspect(self, container_id: str) -> dict[str, Any]:
        require(bool(CONTAINER_ID.fullmatch(container_id)), "invalid_drill_container_id")
        docs = json.loads(self.run(["container", "inspect", container_id]).stdout)
        require(isinstance(docs, list) and len(docs) == 1, "ambiguous_drill_container")
        return docs[0]


class WorldReadWatch:
    """Observe actual reads of existing world files after preflight hashing."""
    def __init__(self, paths: list[Path]):
        libc = ctypes.CDLL(None, use_errno=True)
        libc.inotify_init1.argtypes, libc.inotify_init1.restype = [ctypes.c_int], ctypes.c_int
        libc.inotify_add_watch.argtypes, libc.inotify_add_watch.restype = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32], ctypes.c_int
        self.fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        require(self.fd >= 0, "inotify_unavailable")
        self.expected, self.accessed = set(), set()
        try:
            for path in paths:
                descriptor = libc.inotify_add_watch(self.fd, os.fsencode(path), 0x00000001)  # IN_ACCESS
                require(descriptor >= 0, "world_file_watch_failed")
                self.expected.add(descriptor)
        except Exception:
            self.close()
            raise
        if len(self.expected) != len(paths):
            self.close()
            raise DrillError("world_evidence_files_must_have_distinct_inodes")

    def poll(self) -> bool:
        while True:
            try:
                data = os.read(self.fd, 64 * 1024)
            except BlockingIOError:
                break
            if not data:
                break
            offset = 0
            while offset < len(data):
                require(len(data) - offset >= 16, "inotify_record_invalid")
                descriptor, mask, _cookie, length = struct.unpack_from("iIII", data, offset)
                offset += 16 + length
                require(offset <= len(data) and not mask & 0x00004000, "world_read_evidence_overflow")
                if mask & 0x00000001 and descriptor in self.expected:
                    self.accessed.add(descriptor)
        return self.accessed == self.expected

    def close(self) -> None:
        if getattr(self, "fd", -1) >= 0:
            os.close(self.fd)
            self.fd = -1


def validated_root(value: str | Path) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    root = Path(value)
    require(root.is_absolute() and ".." not in root.parts and "," not in str(root), "absolute_restore_directory_required")
    require(not any(root == forbidden or forbidden in root.parents for forbidden in restore.FORBIDDEN),
            "production_restore_path_rejected")
    for component in [*reversed(root.parents), root]:
        require(component.is_dir() and not component.is_symlink(), "unsafe_restore_directory")
    info = root.lstat()
    require(info.st_uid in (0, os.geteuid()) and info.st_mode & 0o077 == 0, "restore_directory_must_be_private")
    report = remote._private_json(root / "restored.json")
    # Manifests can be larger than small protocol receipts; use the same bound
    # as restore.py's authenticated manifest decryption.
    descriptor = os.open(root / "manifest.json", os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid in (0, os.geteuid()) and info.st_mode & 0o077 == 0,
                "restored_manifest_must_be_private")
        data = stream.read(restore.MAX_MANIFEST_BYTES + 1)
    require(len(data) <= restore.MAX_MANIFEST_BYTES, "restored_manifest_exceeds_limit")
    manifest = remote._read_json(data)
    require(report.get("archive_verified") is True and report.get("runtime_drill_required") is True,
            "verified_archive_restore_required")
    require(report.get("snapshot_id") == manifest.get("snapshot_id") and isinstance(report.get("commit_sha256"), str)
            and remote.SHA_PATTERN.fullmatch(report["commit_sha256"]), "restore_report_identity_mismatch")
    expected = restore.validate_manifest(manifest)
    restore._verify_extracted(root, expected)
    for relative in MOUNTS:
        path = root / "data" / relative
        require(path.is_dir() and not path.is_symlink(), "restored_primary_directory_missing")
    return root, manifest, report


def read_server_config(root: Path, manifest: dict[str, Any]) -> tuple[dict[str, str], bool, list[Path]]:
    name = manifest.get("server_name")
    require(isinstance(name, str) and bool(re.fullmatch(r"[A-Za-z0-9._-]+", name)), "invalid_restored_server_name")
    settings: dict[str, str] = {}
    config_path = root / "data/zomboid/Server" / (name + ".ini")
    require(config_path.is_file() and not config_path.is_symlink(), "restored_server_config_missing")
    for line in config_path.read_text().splitlines():
        if line.lstrip().startswith(("#", ";")):
            continue
        key, separator, value = line.partition("=")
        if separator and key.strip() in ("Mods", "WorkshopItems", "RCONPassword", "RCONPort", "RCONEnabled"):
            require(key.strip() not in settings, "duplicate_server_configuration_key")
            settings[key.strip()] = value.strip()
    require(settings.get("RCONEnabled", "true").lower() == "true", "restored_rcon_disabled")
    resources = remote._private_json(root / "recovery/zomboid-resources.json")
    secrets = [item for item in resources.get("items", []) if item.get("kind") == "Secret"
               and item.get("metadata", {}).get("name") == "pz-runtime"]
    require(len(secrets) == 1, "restored_runtime_secret_missing_or_ambiguous")
    environment = {}
    for key in ("PZ_ADMIN_PASSWORD", "RCON_PASSWORD", "PZ_SERVER_NAME", "PZ_BRANCH", "RCON_PORT"):
        encoded = secrets[0].get("data", {}).get(key)
        if encoded is not None:
            try:
                value = base64.b64decode(encoded, validate=True).decode("utf-8")
            except (ValueError, UnicodeError):
                raise DrillError("invalid_restored_runtime_secret") from None
            require(value and not any(char in value for char in "\x00\n\r"), "unsupported_multiline_runtime_secret")
            environment[key] = value
    require(all(environment.get(key) for key in ("PZ_ADMIN_PASSWORD", "RCON_PASSWORD")), "required_runtime_secret_missing")
    require(environment.get("PZ_SERVER_NAME", name) == name, "runtime_secret_world_name_mismatch")
    require(settings.get("RCONPassword") == environment["RCON_PASSWORD"], "runtime_secret_rcon_mismatch")
    environment.update(PZ_SERVER_NAME=name, RCON_HOST="127.0.0.1", PZ_XMS="4g", PZ_XMX="8g")
    environment.setdefault("RCON_PORT", "27015")
    require(environment["RCON_PORT"] == settings.get("RCONPort", "27015"), "runtime_secret_rcon_port_mismatch")
    world = root / "data/zomboid/Saves/Multiplayer" / name
    paths = [world / filename for filename in WORLD_FILES]
    require(all(path.is_file() and not path.is_symlink() and path.stat().st_size > 0 for path in paths),
            "existing_world_evidence_files_missing")
    configured_mods = bool(settings.get("Mods", "").strip("; ") or settings.get("WorkshopItems", "").strip("; "))
    return environment, configured_mods, paths


def archived_image(root: Path) -> tuple[Path, str]:
    index = remote._private_json(root / "recovery/index.json")
    require(index.get("format") == "pz-recovery-v1" and index.get("complete") is True, "complete_recovery_index_required")
    choices = [item for item in index.get("images", []) if item.get("namespace") == "zomboid"
               and item.get("workload") == "statefulset/zomboid"]
    require(len(choices) == 1, "archived_game_image_ambiguous")
    image = dict(choices[0])
    require(index.get("image_archive", {}).get("path") == "runtime-images.oci.tar", "unexpected_oci_archive_path")
    archive_path = root / "recovery/runtime-images.oci.tar"
    digest = remote._local_digest(archive_path)
    require(all(digest[field] == index["image_archive"].get(field) for field in ("sha256", "size")), "oci_archive_digest_mismatch")
    spec = importlib.util.spec_from_file_location("pz_drill_gather", Path(__file__).with_name("gather-recovery.py"))
    gather = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gather)
    recorded = image.get("exported_manifest_digest")
    gather.verify_oci_archive(archive_path, [image])
    require(recorded == image["exported_manifest_digest"], "oci_manifest_identity_mismatch")
    with tarfile.open(archive_path, "r:") as archive:
        def document(value: str) -> dict[str, Any]:
            require(bool(re.fullmatch(r"sha256:[0-9a-f]{64}", value)), "invalid_oci_descriptor_digest")
            member = archive.getmember("blobs/sha256/" + value.split(":", 1)[1])
            require(member.isfile() and member.size <= 8 * 1024 * 1024, "oci_metadata_invalid")
            return json.load(archive.extractfile(member))
        image_manifest = document(recorded)
        while "manifests" in image_manifest:
            choices = [item for item in image_manifest["manifests"] if item.get("platform", {}).get("os") == "linux"
                       and item.get("platform", {}).get("architecture") == "amd64"]
            require(len(choices) == 1, "oci_amd64_manifest_ambiguous")
            image_manifest = document(choices[0]["digest"])
        config_digest = image_manifest["config"]["digest"]
        config = document(config_digest)
        require(config.get("os") == "linux" and config.get("architecture") == "amd64", "oci_platform_mismatch")
    return archive_path, config_digest


def prepare_offline_mods(root: Path, manifest: dict[str, Any], identifier: str,
                         baseline: dict[str, Any] | None = None) -> dict[str, Any]:
    """Expose recovered Workshop bytes to -nosteam without changing the .ini."""
    settings = {}
    path = root / "data/zomboid/Server" / (manifest["server_name"] + ".ini")
    for line in path.read_text().splitlines():
        key, separator, value = line.partition("=")
        if separator and key in ("Mods", "WorkshopItems"):
            require(key not in settings, "duplicate_server_configuration_key")
            settings[key] = value
    mods = [value.strip() for value in settings.get("Mods", "").split(";") if value.strip()]
    items = [value.strip() for value in settings.get("WorkshopItems", "").split(";") if value.strip()]
    require(len(mods) == len(set(mods)) and len(items) == len(set(items)), "duplicate_configured_mod_or_workshop_item")
    require(all(re.fullmatch(r"[A-Za-z0-9_.-]+", value) for value in mods)
            and all(re.fullmatch(r"[0-9]+", value) for value in items), "unsupported_mod_identifier")
    require(not items or bool(mods), "workshop_items_without_configured_mods")
    expected = restore.validate_manifest(manifest)
    baseline_missing = set()
    if baseline is not None:
        require(baseline.get("format") == "pz-production-mod-baseline-v1"
                and baseline.get("server_name") == manifest["server_name"]
                and baseline.get("config_sha256") == expected[path.relative_to(root).as_posix()]["sha256"]
                and baseline.get("configured_mods") == mods and baseline.get("workshop_items") == items,
                "production_mod_baseline_identity_mismatch")
        require(remote._time(baseline.get("observed_at")) <= remote._time(manifest.get("captured_at")),
                "mod_baseline_must_precede_capture")
        loaded = baseline.get("loaded_mods")
        require(isinstance(loaded, list) and len(loaded) == len(set(loaded)) and set(loaded).issubset(mods),
                "invalid_production_loaded_mod_baseline")
        baseline_missing = set(mods) - set(loaded)
        require(baseline.get("missing_mods") == sorted(baseline_missing)
                and baseline.get("missing_mod_warnings") == sorted(baseline_missing),
                "production_missing_mod_baseline_mismatch")
        classes = baseline.get("loader_class_sha256")
        required_classes = {"zombie/ZomboidFileSystem.class", "zombie/core/znet/SteamWorkshop.class",
                            "zombie/network/GameServer.class", "zombie/network/GameServerWorkshopItems.class",
                            "zombie/gameStates/ChooseGameInfo.class"}
        require(isinstance(classes, dict) and set(classes) == required_classes, "incomplete_mod_loader_baseline")
        with zipfile.ZipFile(root / "data/pz-server/java/projectzomboid.jar") as jar:
            require(all(hashlib.sha256(jar.read(name)).hexdigest() == digest for name, digest in classes.items()),
                    "restored_mod_loader_differs_from_production_baseline")
    roots = [(root / "data/pz-server/steamapps/workshop/content/108600", "/pz-server/steamapps/workshop/content/108600"),
             (root / "data/steam/steamapps/workshop/content/108600", "/home/steam/Steam/steamapps/workshop/content/108600")]
    folders = []
    for item in items:
        choices = [(base / item / "mods", target + "/" + item + "/mods") for base, target in roots
                   if (base / item / "mods").is_dir()]
        require(len(choices) == 1, "restored_workshop_item_missing_or_ambiguous")
        directory, target = choices[0]
        require(not directory.is_symlink(), "unsafe_restored_workshop_directory")
        for folder in sorted(directory.iterdir()):
            require(folder.is_dir() and not folder.is_symlink(), "unsafe_restored_workshop_mod_folder")
            folders.append((item, folder, target + "/" + folder.name))
    local_mods = root / "data/zomboid/mods"
    if local_mods.exists():
        require(local_mods.is_dir() and not local_mods.is_symlink(), "unsafe_restored_local_mod_directory")
        for folder in sorted(local_mods.iterdir()):
            if folder.is_dir() and not folder.is_symlink():
                folders.append((None, folder, None))
    providers = {value: [] for value in mods}
    for item, folder, target in folders:
        candidates = [folder / "mod.info", *folder.glob("*/mod.info")]
        for info in candidates:
            if not info.is_file():
                continue
            relative = info.relative_to(root).as_posix()
            require(not info.is_symlink() and relative in expected and expected[relative]["type"] == "file"
                    and info.stat().st_size <= 1024 * 1024, "unverified_mod_info_rejected")
            values = [line.partition("=")[2].strip() for line in info.read_text().splitlines()
                      if line.partition("=")[0].strip() == "id"]
            require(len(values) == 1, "ambiguous_mod_info_identifier")
            if values[0] in providers:
                providers[values[0]].append({"workshop_id": item, "folder": folder.relative_to(root).as_posix(),
                                           "mod_info": relative, "sha256": expected[relative]["sha256"],
                                           "versioned": info.parent != folder})
    mapping = {}
    for mod, candidates in providers.items():
        versioned = [candidate for candidate in candidates if candidate["versioned"]]
        selected = versioned or candidates
        if mod in baseline_missing:
            require(not selected, "previously_missing_mod_now_has_unverified_effective_state")
            continue
        require(bool(selected), "configured_mod_provenance_missing_or_ambiguous")
        if len({candidate["folder"] for candidate in selected}) > 1:
            require(baseline is not None, "configured_mod_provenance_missing_or_ambiguous")
            # The verified deployed loader walks WorkshopItems in configured
            # order and ChooseGameInfo prefers the first folder index. Existing
            # local mods follow Workshop folders. Same-item duplicates remain
            # ambiguous because filesystem directory order is not reproduced.
            priority = min(items.index(candidate["workshop_id"]) if candidate["workshop_id"] in items else len(items)
                           for candidate in selected)
            selected = [candidate for candidate in selected
                        if (items.index(candidate["workshop_id"]) if candidate["workshop_id"] in items else len(items)) == priority]
            require(len({candidate["folder"] for candidate in selected}) == 1
                    and baseline.get("duplicate_provider_resolution", {}).get(mod) == selected[0]["folder"],
                    "duplicate_mod_provider_selection_not_proven")
        mapping[mod] = selected
    links = []
    selected_folders = {candidate["folder"] for candidates in mapping.values() for candidate in candidates}
    if items:
        local_mods.mkdir(mode=0o755, exist_ok=True)
        for index, (item, folder, target) in enumerate(folders):
            if item is None or folder.relative_to(root).as_posix() not in selected_folders:
                continue
            name = "pz-drill-" + identifier + "-" + item + "-" + str(index)
            link = local_mods / name
            require(not link.exists() and not link.is_symlink(), "offline_mod_link_already_exists")
            link_target = posixpath.relpath(target, "/zomboid/mods")
            link.symlink_to(link_target, target_is_directory=True)
            links.append({"path": link.relative_to(root).as_posix(), "target": link_target,
                          "source": folder.relative_to(root).as_posix(), "workshop_id": item})
    return {"kind": "restored_workshop_links_for_nosteam", "configured_mods": mods,
            "workshop_items": items, "provenance": mapping, "created_links": links,
            "effective_mods": [mod for mod in mods if mod not in baseline_missing],
            "baseline_missing_mods": sorted(baseline_missing), "production_baseline_verified": baseline is not None,
            "production_config_changed": False, "new_mod_bytes_downloaded": False}


def verify_fresh_mod_log(root: Path, adaptation: dict[str, Any]) -> dict[str, Any]:
    path = root / "runtime-drill-game.log"
    required = set(adaptation.get("effective_mods", adaptation["configured_mods"]))
    expected_missing = set(adaptation.get("baseline_missing_mods", []))
    if not required:
        return {"verified": True, "configured_count": 0, "loaded_count": 0, "missing_count": 0}
    if not path.is_file() or path.is_symlink():
        return {"verified": False, "configured_count": len(required), "loaded_count": 0, "missing_count": len(required)}
    loaded, missing_dependencies = set(), set()
    with path.open(errors="replace") as stream:
        for line in stream:
            match = re.fullmatch(r"LOG\s+:\s*Mod\s+[^>\r\n]*> loading ([A-Za-z0-9_.-]+)\s*", line)
            if match:
                loaded.add(match[1])
            missing = re.search(r'required mod "([A-Za-z0-9_.-]+)" not found', line)
            if missing:
                missing_dependencies.add(missing[1])
    return {"verified": required == loaded and missing_dependencies == expected_missing,
            "configured_count": len(required), "loaded_count": len(required & loaded),
            "missing_count": len(required - loaded), "unexpected_loaded_count": len(loaded - required),
            "missing_dependency": bool(missing_dependencies - expected_missing),
            "baseline_warning_reproduced": missing_dependencies == expected_missing,
            "known_missing_mods": sorted(expected_missing)}


def create_arguments(root: Path, identifier: str, image_id: str, env_path: Path, wrapper_name: str) -> list[str]:
    require(bool(re.fullmatch(r"[0-9a-f]{32}", identifier)), "invalid_drill_identity")
    require(bool(re.fullmatch(r"sha256:[0-9a-f]{64}", image_id)), "immutable_image_id_required")
    arguments = ["create", "--name", "pz-restore-drill-" + identifier, "--label", LABEL + "=" + identifier,
                 "--network", "none", "--user", "1000:1000", "--cap-drop", "ALL",
                 "--security-opt", "no-new-privileges", "--memory", "10g", "--memory-swap", "10g",
                 "--cpus", "2", "--pids-limit", "512", "--restart", "no", "--stop-timeout", "300",
                 "--log-driver", "json-file", "--log-opt", "max-size=10m", "--log-opt", "max-file=1",
                 "--env-file", str(env_path), "--entrypoint", "python3"]
    for relative, target in MOUNTS.items():
        arguments += ["--mount", "type=bind,src=" + str(root / "data" / relative) + ",dst=" + target + ",bind-propagation=rprivate"]
    return [*arguments, image_id, "/pz-server/" + wrapper_name]


def owned_container(document: dict[str, Any], container_id: str, identifier: str) -> None:
    require(document.get("Id") == container_id and document.get("Name") == "/pz-restore-drill-" + identifier
            and document.get("Config", {}).get("Labels", {}).get(LABEL) == identifier,
            "drill_container_ownership_mismatch")


def verify_isolation(document: dict[str, Any], root: Path, image_id: str) -> None:
    host = document.get("HostConfig", {})
    require(document.get("Image") == image_id and document.get("Config", {}).get("User") == "1000:1000",
            "drill_image_or_user_mismatch")
    require(host.get("NetworkMode") == "none" and not host.get("PortBindings") and not host.get("PublishAllPorts")
            and not host.get("Privileged") and not host.get("PidMode") and not host.get("UTSMode")
            and host.get("IpcMode") in (None, "", "private"),
            "drill_isolation_invalid")
    require("ALL" in host.get("CapDrop", []) and not host.get("CapAdd")
            and bool({"no-new-privileges", "no-new-privileges=true"}.intersection(host.get("SecurityOpt", []))),
            "drill_capabilities_invalid")
    require(host.get("Memory") == 10 * GIB and host.get("MemorySwap") == 10 * GIB and host.get("NanoCpus") == 2_000_000_000,
            "drill_resource_limits_invalid")
    actual = document.get("Mounts", [])
    require(len(actual) == 3 and all(mount.get("Type") == "bind" for mount in actual), "unexpected_drill_mount")
    expected = {(str(root / "data" / relative), target) for relative, target in MOUNTS.items()}
    require({(mount.get("Source"), mount.get("Destination")) for mount in actual} == expected, "production_or_unexpected_drill_mount")


def capture_fresh_game_log(root: Path, previous: tuple[int, int] | None) -> bool:
    """Preserve only bytes written during this drill; old startup logs are not evidence."""
    path = root / "data/zomboid/server-console-docker.log"
    if not path.exists():
        return False
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        require(stat.S_ISREG(info.st_mode), "drill_game_log_not_regular")
        offset = previous[1] if previous is not None else 0
        if previous is not None and (info.st_ino != previous[0] or info.st_size < offset):
            return False
        if info.st_size - offset > 64 * 1024 * 1024:
            return False
        source.seek(offset)
        output = os.open(root / "runtime-drill-game.log", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(output, "wb") as target:
            remaining = info.st_size - offset
            while remaining:
                block = source.read(min(1024 * 1024, remaining))
                require(bool(block), "drill_game_log_changed_during_capture")
                target.write(block)
                remaining -= len(block)
            target.flush()
            os.fsync(target.fileno())
    return True


def run_drill(restored_dir: str | Path, *, startup_timeout: int = 1800,
              docker: Docker | None = None, mod_baseline: str | Path | None = None) -> dict[str, Any]:
    require(60 <= startup_timeout <= 7200, "startup_timeout_out_of_bounds")
    root, manifest, archive_report = validated_root(restored_dir)
    output = root / "runtime-drill.json"
    require(not output.exists() and not output.is_symlink(), "prior_runtime_drill_requires_new_pristine_restore")
    identifier = uuid.uuid4().hex
    report: dict[str, Any] = {"format": "pz-offline-runtime-drill-v1", "snapshot_id": manifest["snapshot_id"],
                             "commit_sha256": archive_report["commit_sha256"], "drill_id": identifier,
                             "started_at": remote.utc_now(), "mode": "offline-nosteam", "passed": False,
                             "retention_attestation_created": False, "container_removed": False,
                             "checks": {"archive_verified": True, "image_matches_archive": False,
                                        "isolated_runtime": False, "rcon_health": False, "world_file_reads": False,
                                        "mods_verified": False, "clean_shutdown": False}}
    remote._atomic_json(output, report)
    container_id, wrapper, env_path, watcher = None, None, None, None
    console_before = None
    engine = docker
    checks = report["checks"]
    try:
        environment, configured_mods, world_paths = read_server_config(root, manifest)
        checks["mods_verified"] = not configured_mods
        report["mods_result"] = "inconclusive_offline_mods_require_separate_evidence" if configured_mods else "no_mods_or_workshop_configured"
        archive_path, image_id = archived_image(root)
        engine = engine or Docker()
        information = json.loads(engine.run(["info", "--format", "{{json .}}"]).stdout)
        require(information.get("OSType") == "linux" and information.get("Architecture") in ("amd64", "x86_64")
                and information.get("MemTotal", 0) >= 10 * GIB, "docker_host_resources_or_platform_invalid")
        free = os.statvfs(root)
        require(free.f_bavail * free.f_frsize >= 4 * GIB, "insufficient_drill_temporary_disk_budget")
        # Docker load may reject OCI archives on older engines. Keep that as an
        # explicit failure; never pull a mutable replacement image from a registry.
        engine.run(["image", "load", "--input", str(archive_path)], timeout=900)
        images = json.loads(engine.run(["image", "inspect", image_id]).stdout)
        require(len(images) == 1 and images[0].get("Id") == image_id, "loaded_image_config_digest_mismatch")
        checks["image_matches_archive"] = True
        baseline = remote._private_json(Path(mod_baseline)) if mod_baseline is not None else None
        if baseline is not None:
            report["production_mod_baseline"] = baseline
        report["offline_adaptation"] = prepare_offline_mods(root, manifest, identifier, baseline)
        remote._atomic_json(output, report)
        env_path = root / (".drill-" + identifier + ".env")
        descriptor = os.open(env_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write("".join(key + "=" + value + "\n" for key, value in sorted(environment.items())))
            stream.flush()
            os.fsync(stream.fileno())
        wrapper_name = ".pz-restore-drill-" + identifier + ".py"
        wrapper = root / "data/pz-server" / wrapper_name
        descriptor = os.open(wrapper, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o444)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(WRAPPER)
            stream.flush()
            os.fsync(stream.fileno())
        created = engine.run(create_arguments(root, identifier, image_id, env_path, wrapper_name)).stdout.decode().strip()
        require(bool(CONTAINER_ID.fullmatch(created)), "docker_create_returned_invalid_id")
        container_id = created
        report["container_id"] = container_id
        remote._atomic_json(output, report)
        env_path.unlink()  # Already consumed by docker create; never bind mounted.
        env_path = None
        document = engine.inspect(container_id)
        owned_container(document, container_id, identifier)
        verify_isolation(document, root, image_id)
        checks["isolated_runtime"] = True
        watcher = WorldReadWatch(world_paths)
        console_path = root / "data/zomboid/server-console-docker.log"
        if console_path.exists():
            console_info = console_path.lstat()
            require(stat.S_ISREG(console_info.st_mode), "drill_game_log_not_regular")
            console_before = (console_info.st_ino, console_info.st_size)
        engine.run(["container", "start", container_id])
        deadline, consecutive_health = time.monotonic() + startup_timeout, 0
        while time.monotonic() < deadline:
            document = engine.inspect(container_id)
            owned_container(document, container_id, identifier)
            checks["world_file_reads"] = watcher.poll()
            if not document.get("State", {}).get("Running"):
                break
            try:
                result = engine.run(["container", "exec", container_id, "python3", "/usr/local/bin/pz-game", "health"],
                                    timeout=25, check=False)
                consecutive_health = consecutive_health + 1 if result.returncode == 0 else 0
            except subprocess.TimeoutExpired:
                consecutive_health = 0
            checks["rcon_health"] = consecutive_health >= 2
            checks["world_file_reads"] = watcher.poll()
            if checks["rcon_health"] and checks["world_file_reads"]:
                break
            time.sleep(5)
        if not checks["rcon_health"] or not checks["world_file_reads"]:
            report["failure"] = "runtime_health_or_existing_world_reads_unconfirmed"
    except KeyboardInterrupt:
        report["failure"] = "runtime_drill_interrupted"
    except (DrillError, restore.RestoreError, remote.BackupError) as error:
        report["failure"] = str(error)
    except Exception as error:
        report["failure"] = "runtime_drill_failed_" + type(error).__name__
    finally:
        if watcher is not None:
            watcher.close()
        if env_path is not None:
            env_path.unlink(missing_ok=True)
        if container_id is not None and engine is not None:
            try:
                document = engine.inspect(container_id)
                owned_container(document, container_id, identifier)
                requested_stop = document.get("State", {}).get("Running") is True
                if requested_stop:
                    engine.run(["container", "kill", "--signal", "SIGTERM", container_id])
                    deadline = time.monotonic() + 300
                    while time.monotonic() < deadline:
                        document = engine.inspect(container_id)
                        owned_container(document, container_id, identifier)
                        if not document.get("State", {}).get("Running"):
                            break
                        time.sleep(2)
                state = document.get("State", {})
                stopped = state.get("Running") is False
                checks["clean_shutdown"] = requested_stop and stopped and state.get("ExitCode") == 0 and not state.get("OOMKilled")
                if stopped:
                    report["fresh_game_log_captured"] = capture_fresh_game_log(root, console_before)
                    if report["fresh_game_log_captured"]:
                        report["mods_evidence"] = verify_fresh_mod_log(root, report["offline_adaptation"])
                        checks["mods_verified"] = report["mods_evidence"]["verified"]
                        report["mods_result"] = "verified_fresh_loader_messages" if checks["mods_verified"] else "inconclusive_missing_fresh_loader_evidence"
                    # Preserve private diagnostics on this disposable restored FS.
                    logs = engine.run(["container", "logs", "--tail", "2000", container_id], check=False).stdout
                    descriptor = os.open(root / "runtime-drill-private.log", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    with os.fdopen(descriptor, "wb") as stream:
                        stream.write(logs)
                        stream.flush()
                        os.fsync(stream.fileno())
                    engine.run(["container", "rm", container_id])
                    report["container_removed"] = True
                else:
                    report["failure"] = "isolated_drill_container_did_not_stop_operator_inspection_required"
                    # Never force-kill a slow world save, never clean other containers.
            except Exception as error:
                report["cleanup_failure"] = type(error).__name__
        if wrapper is not None and wrapper.exists() and (container_id is None or report["container_removed"]):
            if wrapper.is_file() and not wrapper.is_symlink() and hashlib.sha256(wrapper.read_bytes()).digest() == hashlib.sha256(WRAPPER).digest():
                wrapper.unlink()
        report["completed_at"] = remote.utc_now()
        report["passed"] = all(checks.values()) and report["container_removed"] and "failure" not in report and "cleanup_failure" not in report
        remote._atomic_json(output, report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--restored-dir", required=True, help="Absolute path to a NEW verified restore; its data will change")
    parser.add_argument("--startup-timeout", type=int, default=1800)
    parser.add_argument("--mod-baseline", help="Private pre-backup production loader evidence, bound to config and game bytecode")
    args = parser.parse_args(argv)
    def interrupted(_signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    try:
        report = run_drill(args.restored_dir, startup_timeout=args.startup_timeout, mod_baseline=args.mod_baseline)
        print(json.dumps({"snapshot_id": report["snapshot_id"], "passed": report["passed"],
                          "checks": report["checks"], "container_removed": report["container_removed"],
                          "retention_attestation_created": False}, sort_keys=True))
        return 0 if report["passed"] else 1
    except (DrillError, restore.RestoreError, remote.BackupError) as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception as error:
        print("runtime_drill_failed_" + type(error).__name__, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
