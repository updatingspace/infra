#!/usr/bin/env python3
"""Give archived Portal images local identities; preserve every application layer."""
import argparse
import copy
import hashlib
import io
import json
from pathlib import Path
import tarfile


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def digest(data):
    return 'sha256:' + hashlib.sha256(data).hexdigest()


def blob_path(value):
    assert value.startswith('sha256:') and len(value) == 71
    return 'blobs/sha256/' + value[7:]


def localize(source, target, images):
    assert source.resolve() != target.resolve() and not target.exists()
    blobs, descriptors, result = {}, [], {}
    with tarfile.open(source) as archive:
        members = {member.name: member for member in archive.getmembers()}
        assert len(members) == len(archive.getmembers())

        def read_blob(descriptor):
            data = archive.extractfile(blob_path(descriptor['digest'])).read()
            assert len(data) == descriptor['size'] and digest(data) == descriptor['digest']
            return json.loads(data)

        index = json.load(archive.extractfile('index.json'))
        for service, reference in images.items():
            original = next(x for x in index['manifests'] if x['digest'] == reference.split('@')[1])
            manifest = read_blob(original)
            original_config = read_blob(manifest['config'])
            config = copy.deepcopy(original_config)
            config.setdefault('config', {}).setdefault('Labels', {})['com.updspace.source-image'] = reference
            restored = copy.deepcopy(config)
            restored['config']['Labels'].pop('com.updspace.source-image')
            if 'Labels' not in original_config['config']:
                del restored['config']['Labels']
            assert restored == original_config
            config_data = encode(config)
            blobs[blob_path(digest(config_data))] = config_data
            local_manifest = copy.deepcopy(manifest)
            local_manifest['config'].update(digest=digest(config_data), size=len(config_data))
            assert local_manifest['layers'] == manifest['layers']
            manifest_data = encode(local_manifest)
            manifest_digest = digest(manifest_data)
            blobs[blob_path(manifest_digest)] = manifest_data
            local_reference = f'localhost/updspace/portal-{service}@{manifest_digest}'
            descriptors.append({'mediaType': original['mediaType'], 'digest': manifest_digest,
                'size': len(manifest_data), 'annotations': {
                    'io.containerd.image.name': local_reference,
                    'org.opencontainers.image.ref.name': local_reference}})
            result[service] = local_reference
        assert len(result) == 8
        with tarfile.open(target, 'x') as output:
            for member in archive.getmembers():
                if member.name in ('index.json', 'manifest.json'):
                    continue
                if member.isfile():
                    stream = archive.extractfile(member)
                    if member.name.startswith('blobs/sha256/'):
                        hasher = hashlib.sha256()
                        while chunk := stream.read(1024 * 1024):
                            hasher.update(chunk)
                        assert hasher.hexdigest() == member.name.rsplit('/', 1)[1]
                        stream.seek(0)
                    output.addfile(member, stream)
                else:
                    assert member.isdir()
                    output.addfile(member)
            blobs['index.json'] = encode({'schemaVersion': 2,
                'mediaType': 'application/vnd.oci.image.index.v1+json', 'manifests': descriptors})
            for name, data in blobs.items():
                member = tarfile.TarInfo(name)
                member.size, member.mode = len(data), 0o644
                output.addfile(member, io.BytesIO(data))
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('target', type=Path)
    parser.add_argument('source_images', type=Path)
    parser.add_argument('local_images', type=Path)
    args = parser.parse_args()
    result = localize(args.source, args.target, json.loads(args.source_images.read_text()))
    args.local_images.write_text(json.dumps(result, indent=2) + '\n')
    print('Verified all source blobs; localized 8 images with unchanged layers and runtime configuration')
