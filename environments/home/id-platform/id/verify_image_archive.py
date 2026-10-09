#!/usr/bin/env python3
"""Verify every OCI blob and the linux/amd64 dependency closure of pinned ID images."""
import argparse
import hashlib
import json
from pathlib import Path
import tarfile


def verify(archive, expected_images):
    blobs, documents, names = set(), {}, set()
    with tarfile.open(archive, 'r|*') as source:
        for member in source:
            if not member.isfile():
                continue
            if member.name in names:
                raise ValueError('Duplicate archive member')
            names.add(member.name)
            stream = source.extractfile(member)
            if member.name.startswith('blobs/sha256/'):
                digest = member.name.removeprefix('blobs/sha256/')
                hasher = hashlib.sha256()
                small = bytearray()
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    hasher.update(block)
                    if member.size < 4 * 1024 * 1024:
                        small.extend(block)
                if hasher.hexdigest() != digest:
                    raise ValueError('OCI blob SHA256 mismatch')
                blobs.add('sha256:' + digest)
                try:
                    documents['sha256:' + digest] = json.loads(small)
                except (ValueError, UnicodeDecodeError):
                    pass
            elif member.name == 'index.json':
                index = json.load(stream)
    if 'index.json' not in names:
        raise ValueError('Missing OCI index')
    refs = {x.get('annotations', {}).get('io.containerd.image.name'): x for x in index['manifests']}

    def visit(descriptor):
        digest = descriptor['digest']
        if digest not in blobs:
            raise ValueError('Missing required image blob')
        if digest not in documents:
            return
        document = documents[digest]
        if not isinstance(document, dict):
            return
        if 'manifests' in document:
            children = [x for x in document['manifests'] if x.get('platform', {}).get('os') == 'linux'
                        and x.get('platform', {}).get('architecture') == 'amd64']
            if not children:
                raise ValueError('No linux/amd64 manifest')
            for child in children:
                visit(child)
        elif 'layers' in document:
            visit(document['config'])
            for layer in document['layers']:
                visit(layer)

    for image in expected_images:
        descriptor = refs.get(image)
        if not descriptor or descriptor['digest'] != image.split('@', 1)[1]:
            raise ValueError('Missing or changed pinned image')
        visit(descriptor)
    return {'verified_images': len(expected_images), 'verified_blobs': len(blobs),
            'platform': 'linux/amd64', 'required_blob_closure_complete': True}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    parser.add_argument('manifest', type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.archive, json.loads(args.manifest.read_text())['images'])))
