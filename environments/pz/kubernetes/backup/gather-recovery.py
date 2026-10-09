#!/usr/bin/env python3
"""Collect recovery dependencies into the private staging tree (never stdout)."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tarfile
import time

NAMESPACES = ('zomboid', 'edge', 'observability')
WORKLOADS = {'zomboid': ('statefulset/zomboid', 'deployment/panel'),
             'edge': ('deployment/caddy',), 'observability': ('deployment/otel-collector',)}
DATA = Path('/srv/pz-storage/zomboid')
INFRASTRUCTURE = Path('/opt/pz-infrastructure')
BACKUP_CODE = Path('/opt/pz-backup')
SOURCE_EXCLUDED_DIRECTORIES = {'private', '__pycache__', 'provider-mirror'}


def run(args, timeout=120):
    p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if p.returncode:
        raise RuntimeError('Recovery dependency command failed (output withheld)')
    return p.stdout


def kube(ns, args):
    return json.loads(run(['k3s', 'kubectl', '--kubeconfig', '/etc/rancher/k3s/operator.yaml',
                           '--request-timeout=30s', '-n', ns, *args, '-o', 'json']))


def write_json(path, value):
    with path.open('x', encoding='utf-8') as f:
        json.dump(value, f, sort_keys=True, separators=(',', ':'))
        f.write('\n'); f.flush(); os.fsync(f.fileno())


def hash_file(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(4 * 1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def digest_from_image_id(value):
    value = value.removeprefix('docker-pullable://').removeprefix('containerd://')
    match = re.search(r'(?:^|@)(sha256:[0-9a-f]{64})\Z', value)
    if not match:
        raise RuntimeError('Runtime image digest is unavailable')
    return match.group(1)


def verify_oci_archive(path, images):
    """Hash the exported OCI bytes and bind running imageIDs to their graphs.

    A successful ctr export of a mutable tag alone does not prove it contains
    the image currently running. Match each runtime config/manifest/index digest
    to the exact exported reference's reachable OCI graph before completion.
    """
    with tarfile.open(path, mode='r:') as archive:
        members, blobs = {}, {}
        for member in archive.getmembers():
            name = member.name.rstrip('/')
            if name in members or name.startswith('/') or '..' in Path(name).parts or not (member.isdir() or member.isfile()):
                raise RuntimeError('Unsafe OCI archive entry')
            members[name] = member
            if name.startswith('blobs/') and member.isfile():
                if not re.fullmatch(r'blobs/sha256/[0-9a-f]{64}', name):
                    raise RuntimeError('Unsupported OCI digest algorithm')
                h = hashlib.sha256()
                with archive.extractfile(member) as stream:
                    for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
                        h.update(block)
                if h.hexdigest() != name.rsplit('/', 1)[1]:
                    raise RuntimeError('OCI blob digest mismatch')
                blobs['sha256:' + h.hexdigest()] = {'path': name, 'size': member.size}

        def document(name):
            member = members.get(name)
            if member is None or not member.isfile() or member.size > 8 * 1024 * 1024:
                raise RuntimeError('OCI metadata unavailable')
            with archive.extractfile(member) as stream:
                value = json.load(stream)
            if not isinstance(value, dict):
                raise RuntimeError('OCI metadata invalid')
            return value

        if document('oci-layout').get('imageLayoutVersion') != '1.0.0':
            raise RuntimeError('OCI layout unsupported')
        index = document('index.json')
        references = {}
        for descriptor in index.get('manifests', []):
            annotations = descriptor.get('annotations', {})
            for key in ('io.containerd.image.name', 'org.opencontainers.image.ref.name'):
                if annotations.get(key):
                    references.setdefault(annotations[key], []).append(descriptor)

        def reachable(descriptor, seen=None):
            seen = set() if seen is None else seen
            digest = descriptor.get('digest')
            if digest in seen:
                return seen
            blob = blobs.get(digest)
            if blob is None or blob['size'] != descriptor.get('size'):
                raise RuntimeError('OCI graph has missing content')
            seen.add(digest)
            media_type = descriptor.get('mediaType', '')
            if 'manifest' in media_type or 'index' in media_type:
                body = document(blob['path'])
                if 'manifests' in body:
                    selected = [item for item in body['manifests']
                                if item.get('platform', {}).get('os') == 'linux'
                                and item.get('platform', {}).get('architecture') == 'amd64']
                    if len(selected) != 1:
                        raise RuntimeError('OCI amd64 manifest missing or ambiguous')
                    reachable(selected[0], seen)
                else:
                    reachable(body['config'], seen)
                    for layer in body['layers']:
                        reachable(layer, seen)
            return seen

        for image in images:
            ref = image['export_ref']
            choices = references.get(ref, [])
            if not choices:
                raise RuntimeError('Exported image reference is missing')
            roots = {item['digest']: item for item in choices}
            if len(roots) != 1:
                raise RuntimeError('Exported image reference is ambiguous')
            descriptor = next(iter(roots.values()))
            graph = reachable(descriptor)
            runtime_digest = digest_from_image_id(image['image_id']) if image.get('image_id') else None
            if runtime_digest and runtime_digest not in graph:
                raise RuntimeError('Exported image does not match the running image')
            image['exported_manifest_digest'] = descriptor['digest']
            image['runtime_digest_verified'] = runtime_digest is not None
            image['oci_reference'] = ref
        return {'verified_blob_count': len(blobs), 'verified_image_count': len(images)}


def game_artifacts():
    """Record available exact build/mod metadata; all original bytes stay in data/."""
    metadata = {'steam_manifests': [], 'server_settings': [], 'workshop_items': []}
    for pattern in ('pz-server/steamapps/appmanifest_380870.acf', 'steam/**/appmanifest_380870.acf',
                    'pz-server/steamapps/workshop/appworkshop_108600.acf', 'steam/**/appworkshop_108600.acf'):
        for path in sorted(DATA.glob(pattern)):
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
                raise RuntimeError('Steam metadata file is unsafe')
            raw = path.read_text(errors='replace')
            builds = re.findall(r'"buildid"\s+"([0-9]+)"', raw, flags=re.I)
            metadata['steam_manifests'].append({'path': str(path.relative_to(DATA)), 'sha256': hash_file(path),
                                                 'build_ids': sorted(set(builds))})
    server = DATA / 'zomboid/Server'
    if not server.is_dir() or server.is_symlink():
        raise RuntimeError('Server configuration directory unavailable')
    for path in sorted(server.glob('*.ini')):
        if path.is_symlink() or path.stat().st_size > 16 * 1024 * 1024:
            raise RuntimeError('Server configuration file is unsafe')
        selected = {}
        for line in path.read_text(errors='replace').splitlines():
            key, separator, value = line.partition('=')
            if separator and key.strip() in {'Mods', 'WorkshopItems', 'Map'}:
                selected[key.strip()] = value.strip()
        metadata['server_settings'].append({'path': str(path.relative_to(DATA)), 'sha256': hash_file(path), **selected})
    for parent in (DATA / 'pz-server/steamapps/workshop/content/108600', DATA / 'steam/steamapps/workshop/content/108600'):
        if parent.is_symlink():
            raise RuntimeError('Workshop directory is unsafe')
        if parent.is_dir():
            for path in sorted(parent.iterdir()):
                if path.is_dir() and path.name.isdigit() and not path.is_symlink():
                    metadata['workshop_items'].append({'workshop_id': path.name, 'path': str(path.relative_to(DATA))})
    if not metadata['server_settings']:
        raise RuntimeError('Server settings metadata unavailable')
    # Some manually installed Steam builds have no ACF. Absence is explicit;
    # complete binaries are included in data/pz-server and SHA256 manifest.
    metadata['steam_build_metadata_available'] = bool(metadata['steam_manifests'])
    return metadata


def copy_sources(deployed, destination):
    if deployed.is_symlink() or not deployed.is_dir():
        raise RuntimeError('Trusted infrastructure source unavailable')
    destination.mkdir(mode=0o700)
    allowed = {'.py', '.tf', '.hcl', '.yaml', '.yml', '.mjs', '.sh', '.md', '.service', '.timer'}
    copied = 0
    for base, dirs, files in os.walk(deployed, followlinks=False):
        info = Path(base).stat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise RuntimeError('Infrastructure directory is not root controlled')
        dirs[:] = [d for d in dirs if not d.startswith('.') and d not in SOURCE_EXCLUDED_DIRECTORIES
                   and not (Path(base) / d).is_symlink()]
        for name in files:
            src = Path(base) / name
            if src.is_symlink() or (src.suffix not in allowed and name not in {'requirements.txt', 'requirements.lock.txt'}) or name.endswith('.tfvars') or name.startswith('test_'):
                continue
            info = src.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                raise RuntimeError('Infrastructure source is not root controlled')
            rel = src.relative_to(deployed)
            dst = destination / rel
            dst.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copy2(src, dst)
            dst.chmod(0o600)
            copied += 1
    if not copied:
        raise RuntimeError('Recovery source tree is empty')
    return copied


def collect(output):
    os.umask(0o077)
    if output.is_symlink() or output.exists() and any(output.iterdir()):
        raise RuntimeError('Recovery output must be an empty private directory')
    output.mkdir(parents=False, exist_ok=True, mode=0o700)
    secrets, images, image_refs = [], [], set()
    available_refs = set(run(['k3s', 'ctr', '-n', 'k8s.io', 'images', 'ls', '-q']).decode().splitlines())
    for ns in NAMESPACES:
        resources = kube(ns, ['get', 'secrets,configmaps,deployments,statefulsets,services,persistentvolumeclaims,cronjobs,roles,rolebindings,networkpolicies'])
        write_json(output / (ns + '-resources.json'), resources)
        secrets.extend({'namespace': ns, 'name': x['metadata']['name']}
                       for x in resources['items'] if x['kind'] == 'Secret')
        by_key = {(x['kind'].lower(), x['metadata']['name']): x for x in resources['items']}
        pods = kube(ns, ['get', 'pods'])['items']
        for resource in WORKLOADS[ns]:
            kind, name = resource.split('/')
            obj = by_key.get((kind, name))
            if obj is None:
                raise RuntimeError('Required recovery workload is missing')
            selector = obj['spec']['selector']['matchLabels']
            matching = [p for p in pods if all(p['metadata'].get('labels', {}).get(k) == v for k, v in selector.items())
                        and not p['metadata'].get('deletionTimestamp') and p['status'].get('phase') == 'Running']
            configured = [c['image'] for c in obj['spec']['template']['spec']['containers']]
            for pod in matching:
                statuses = pod['status'].get('containerStatuses', [])
                if {item['name'] for item in statuses} != {item['name'] for item in pod['spec']['containers']}:
                    raise RuntimeError('Running workload container status is incomplete')
                for c in statuses:
                    if 'running' not in c.get('state', {}):
                        raise RuntimeError('Recovery workload container is not running')
                    image_id = c.get('imageID', '')
                    runtime_digest = digest_from_image_id(image_id)
                    configured_ref = next(v['image'] for v in pod['spec']['containers'] if v['name'] == c['name'])
                    runtime_ref = image_id.removeprefix('docker-pullable://').removeprefix('containerd://')
                    candidates = [ref for ref in (runtime_ref, runtime_digest) if ref in available_refs]
                    export_ref = candidates[0] if candidates else configured_ref
                    image_refs.add(export_ref)
                    images.append({'namespace': ns, 'workload': resource, 'container': c['name'],
                                   'configured_image': configured_ref, 'export_ref': export_ref,
                                   'image_id': image_id})
            if not matching:
                if obj['spec'].get('replicas', 1) != 0:
                    raise RuntimeError('Expected running recovery workload has no running pod')
                for ref in configured:
                    image_refs.add(ref)
                    images.append({'namespace': ns, 'workload': resource, 'configured_image': ref,
                                   'export_ref': ref, 'image_id': None, 'intentionally_stopped': True})
    # Running workloads use their available runtime references. A configured
    # tag@digest alias need not exist in containerd even when its exact runtime
    # image is present. verify_oci_archive binds each export to the running ID;
    # configured references remain in the saved resources and image metadata.
    archive = output / 'runtime-images.oci.tar'
    run(['k3s', 'ctr', '-n', 'k8s.io', 'images', 'export', '--platform', 'linux/amd64', str(archive), *sorted(image_refs)], timeout=900)
    verified = verify_oci_archive(archive, images)
    with archive.open('rb') as f:
        os.fsync(f.fileno())
    copied = copy_sources(INFRASTRUCTURE, output / 'infrastructure')
    backup_copied = copy_sources(BACKUP_CODE, output / 'backup-code')
    write_json(output / 'game-artifacts.json', game_artifacts())
    if not copied or not secrets or not images:
        raise RuntimeError('Recovery dependencies incomplete')
    write_json(output / 'index.json', {'complete': True, 'format': 'pz-recovery-v1', 'captured_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                                      'images': images, 'secrets': secrets, 'image_archive': {'path': archive.name, 'size': archive.stat().st_size, 'sha256': hash_file(archive)},
                                      'infrastructure_file_count': copied, 'backup_code_file_count': backup_copied,
                                      'oci_verification': verified, 'game_metadata': 'game-artifacts.json',
                                      'excluded': ['Terraform state/plan and old migration archive: independent operator recovery set',
                                                   'infrastructure/provider-mirror: downloaded Terraform provider cache; rebuild from the preserved provider lock file']})
    fd = os.open(output, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        collect(args.output)
    except Exception as exc:
        raise SystemExit('Recovery capture failed: ' + type(exc).__name__) from None
