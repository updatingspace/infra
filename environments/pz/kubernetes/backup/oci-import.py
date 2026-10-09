#!/usr/bin/env python3
"""Stream one verified OCI image as Docker's tar format, preserving config ID."""
from __future__ import annotations
import argparse
import gzip
import hashlib
import io
import json
import re
import sys
import tarfile
from typing import Any, BinaryIO

SHA = re.compile(r'sha256:[0-9a-f]{64}\Z')

class ImportError(RuntimeError):
    pass

def require(value: bool, reason: str) -> None:
    if not value:
        raise ImportError(reason)

class DigestReader:
    def __init__(self, stream: BinaryIO):
        self.stream, self.sha256, self.size = stream, hashlib.sha256(), 0
    def read(self, size=-1):
        data = self.stream.read(size)
        self.sha256.update(data)
        self.size += len(data)
        return data
    def digest(self):
        return 'sha256:' + self.sha256.hexdigest()

def stream_docker_archive(path: str, config_digest: str, destination: BinaryIO) -> dict[str, Any]:
    require(bool(SHA.fullmatch(config_digest)), 'invalid_config_digest')
    with tarfile.open(path, 'r:') as source:
        names = [member.name for member in source.getmembers()]
        require(len(names) == len(set(names)), 'duplicate_oci_archive_member')
        def raw(descriptor):
            digest = descriptor['digest']
            require(bool(SHA.fullmatch(digest)), 'invalid_oci_digest')
            member = source.getmember('blobs/sha256/' + digest[7:])
            require(member.isfile() and member.size == descriptor['size'] and member.size <= 8 * 1024**2,
                    'invalid_oci_metadata_blob')
            data = source.extractfile(member).read()
            require('sha256:' + hashlib.sha256(data).hexdigest() == digest, 'oci_metadata_hash_mismatch')
            return data
        index = json.load(source.extractfile('index.json'))
        matches = []
        def visit(descriptor):
            doc = json.loads(raw(descriptor))
            if 'manifests' in doc:
                for child in doc['manifests']:
                    if child.get('platform', {}).get('os') == 'linux' and child.get('platform', {}).get('architecture') == 'amd64':
                        visit(child)
            elif doc.get('config', {}).get('digest') == config_digest:
                matches.append(doc)
        for descriptor in index['manifests']:
            visit(descriptor)
        require(bool(matches) and all(doc == matches[0] for doc in matches), 'game_image_manifest_missing_or_ambiguous')
        manifest = matches[0]
        config_bytes = raw(manifest['config'])
        config = json.loads(config_bytes)
        require(config.get('os') == 'linux' and config.get('architecture') == 'amd64', 'game_platform_mismatch')
        layers, diff_ids = manifest['layers'], config.get('rootfs', {}).get('diff_ids')
        require(isinstance(diff_ids, list) and len(diff_ids) == len(layers), 'image_rootfs_layer_count_mismatch')
        def layer_reader(descriptor):
            digest = descriptor['digest']
            require(bool(SHA.fullmatch(digest)), 'invalid_layer_digest')
            member = source.getmember('blobs/sha256/' + digest[7:])
            require(member.isfile() and member.size == descriptor['size'], 'invalid_layer_blob')
            packed = DigestReader(source.extractfile(member))
            kind = descriptor.get('mediaType', '')
            if kind.endswith('+gzip') or kind == 'application/vnd.docker.image.rootfs.diff.tar.gzip':
                return packed, DigestReader(gzip.GzipFile(fileobj=packed, mode='rb'))
            require(kind in ('application/vnd.oci.image.layer.v1.tar', 'application/vnd.docker.image.rootfs.diff.tar'),
                    'unsupported_oci_layer_compression')
            return packed, DigestReader(packed)
        sizes = []
        for descriptor, diff_id in zip(layers, diff_ids):
            packed, unpacked = layer_reader(descriptor)
            while unpacked.read(1024**2):
                pass
            while packed.read(1024**2):
                pass
            require(unpacked.digest() == diff_id and packed.digest() == descriptor['digest'] and packed.size == descriptor['size'],
                    'oci_layer_or_diff_id_mismatch')
            sizes.append(unpacked.size)
        config_name = config_digest[7:] + '.json'
        layer_names = ['layers/' + str(index) + '.tar' for index in range(len(layers))]
        docker_manifest = json.dumps([{'Config': config_name, 'RepoTags': [], 'Layers': layer_names}], separators=(',', ':')).encode()
        with tarfile.open(fileobj=destination, mode='w|') as target:
            def add_bytes(name, content):
                info = tarfile.TarInfo(name)
                info.size, info.mode = len(content), 0o444
                target.addfile(info, io.BytesIO(content))
            add_bytes(config_name, config_bytes)
            for name, descriptor, diff_id, size in zip(layer_names, layers, diff_ids, sizes):
                packed, unpacked = layer_reader(descriptor)
                info = tarfile.TarInfo(name)
                info.size, info.mode = size, 0o444
                target.addfile(info, unpacked)
                require(not unpacked.read(1), 'oci_layer_changed_during_import')
                while packed.read(1024**2):
                    pass
                require(unpacked.digest() == diff_id and packed.digest() == descriptor['digest'],
                        'oci_layer_changed_during_import')
            add_bytes('manifest.json', docker_manifest)
    return {'config_digest': config_digest, 'layers': len(layers), 'uncompressed_layer_bytes': sum(sizes)}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', required=True)
    parser.add_argument('--config-digest', required=True)
    args = parser.parse_args()
    try:
        stream_docker_archive(args.archive, args.config_digest, sys.stdout.buffer)
        return 0
    except Exception as error:
        print('OCI import failed: ' + type(error).__name__, file=sys.stderr)
        return 1

if __name__ == '__main__':
    raise SystemExit(main())
