#!/usr/bin/env python3
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('gather_recovery', Path(__file__).with_name('gather-recovery.py'))
g = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(g)


class RecoveryArtifactsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def archive(self, *, corrupt=False, missing=False):
        blobs = {}

        def blob(data, media):
            if not isinstance(data, bytes):
                data = json.dumps(data, sort_keys=True).encode()
            digest = 'sha256:' + hashlib.sha256(data).hexdigest()
            blobs['blobs/sha256/' + digest[7:]] = data
            return {'mediaType': media, 'digest': digest, 'size': len(data)}

        config = blob({'architecture': 'amd64', 'os': 'linux'}, 'application/vnd.oci.image.config.v1+json')
        layer = blob(b'rootfs data', 'application/vnd.oci.image.layer.v1.tar')
        manifest = blob({'schemaVersion': 2, 'config': config, 'layers': [layer]}, 'application/vnd.oci.image.manifest.v1+json')
        manifest['annotations'] = {'io.containerd.image.name': 'local/pz:live'}
        if corrupt:
            blobs['blobs/sha256/' + layer['digest'][7:]] = b'bad contents'
        if missing:
            del blobs['blobs/sha256/' + layer['digest'][7:]]
        entries = {'oci-layout': json.dumps({'imageLayoutVersion': '1.0.0'}).encode(),
                   'index.json': json.dumps({'schemaVersion': 2, 'manifests': [manifest]}).encode(), **blobs}
        path = self.root / 'images.tar'
        with tarfile.open(path, 'w') as archive:
            for name, raw in entries.items():
                info = tarfile.TarInfo(name)
                info.size = len(raw)
                archive.addfile(info, io.BytesIO(raw))
        return path, config['digest']

    def test_exact_runtime_config_digest_and_all_blob_hashes_verified(self):
        path, digest = self.archive()
        image = {'export_ref': 'local/pz:live', 'image_id': digest}
        result = g.verify_oci_archive(path, [image])
        self.assertEqual(result['verified_blob_count'], 3)
        self.assertTrue(image['runtime_digest_verified'])
        self.assertEqual(image['oci_reference'], 'local/pz:live')

    def test_exporting_mutated_tag_is_not_proof_of_running_image(self):
        path, _ = self.archive()
        with self.assertRaisesRegex(RuntimeError, 'does not match the running image'):
            g.verify_oci_archive(path, [{'export_ref': 'local/pz:live', 'image_id': 'sha256:' + 'a' * 64}])

    def test_corrupt_and_incomplete_oci_archives_rejected(self):
        path, digest = self.archive(corrupt=True)
        with self.assertRaisesRegex(RuntimeError, 'digest mismatch'):
            g.verify_oci_archive(path, [{'export_ref': 'local/pz:live', 'image_id': digest}])
        path, digest = self.archive(missing=True)
        with self.assertRaisesRegex(RuntimeError, 'missing content'):
            g.verify_oci_archive(path, [{'export_ref': 'local/pz:live', 'image_id': digest}])

    def test_intentionally_stopped_image_is_exported_but_not_claimed_runtime_verified(self):
        path, _ = self.archive()
        image = {'export_ref': 'local/pz:live', 'image_id': None, 'intentionally_stopped': True}
        g.verify_oci_archive(path, [image])
        self.assertFalse(image['runtime_digest_verified'])
        self.assertIn('exported_manifest_digest', image)

    def test_replicas_one_without_running_pod_blocks_complete_marker(self):
        resource = {'kind': 'StatefulSet', 'metadata': {'name': 'zomboid'}, 'spec': {
            'replicas': 1, 'selector': {'matchLabels': {'app': 'game'}},
            'template': {'spec': {'containers': [{'name': 'zomboid', 'image': 'local/pz:live'}]}}}}
        def kube(_namespace, args):
            return {'items': []} if args == ['get', 'pods'] else {'items': [resource]}
        with patch.object(g, 'run', return_value=b'local/pz:live\n'), patch.object(g, 'kube', side_effect=kube):
            with self.assertRaisesRegex(RuntimeError, 'no running pod'):
                g.collect(self.root / 'capture')
        self.assertFalse((self.root / 'capture/index.json').exists())

    def test_game_metadata_captures_build_mods_and_workshop_without_passwords(self):
        (self.root / 'pz-server/steamapps').mkdir(parents=True)
        (self.root / 'pz-server/steamapps/appmanifest_380870.acf').write_text('"AppState" { "buildid" "123456" }')
        (self.root / 'zomboid/Server').mkdir(parents=True)
        (self.root / 'zomboid/Server/world.ini').write_text('Mods=Mod1;Mod2\nWorkshopItems=123;456\nMap=Muldraugh, KY\nRCONPassword=PRIVATE\n')
        with patch.object(g, 'DATA', self.root):
            value = g.game_artifacts()
        self.assertEqual(value['steam_manifests'][0]['build_ids'], ['123456'])
        self.assertEqual(value['server_settings'][0]['WorkshopItems'], '123;456')
        self.assertNotIn('PRIVATE', json.dumps(value))

    def test_untrusted_provider_cache_is_excluded_while_real_sources_remain_checked(self):
        source = self.root / 'infrastructure'
        source.mkdir()
        code = source / 'main.tf'
        code.write_text('terraform {}\n')
        lock = source / '.terraform.lock.hcl'
        lock.write_text('provider "hashicorp/kubernetes" {}\n')
        cache = source / 'provider-mirror/registry.terraform.io/hashicorp/kubernetes/3.2.1/linux_amd64'
        cache.mkdir(parents=True)
        (cache / 'terraform-provider-kubernetes').write_bytes(b'downloaded binary owned by deploy user')
        destination = self.root / 'captured'
        original_stat = Path.stat
        def trusted_sources(path, *args, **kwargs):
            info = original_stat(path, *args, **kwargs)
            if path in {source, code, lock}:
                return SimpleNamespace(st_uid=0, st_mode=info.st_mode & ~0o022)
            if path.is_relative_to(source / 'provider-mirror'):
                raise AssertionError('excluded provider cache must not be inspected as source')
            return info
        with patch.object(Path, 'stat', trusted_sources):
            count = g.copy_sources(source, destination)
        self.assertEqual(count, 2)
        self.assertEqual((destination / 'main.tf').read_text(), 'terraform {}\n')
        self.assertTrue((destination / '.terraform.lock.hcl').exists())
        self.assertFalse((destination / 'provider-mirror').exists())

    def test_untrusted_real_source_file_is_still_rejected(self):
        source = self.root / 'source'
        source.mkdir()
        code = source / 'main.tf'
        code.write_text('terraform {}\n')
        original_stat = Path.stat
        def ownership(path, *args, **kwargs):
            info = original_stat(path, *args, **kwargs)
            if path == source:
                return SimpleNamespace(st_uid=0, st_mode=info.st_mode & ~0o022)
            if path == code:
                return SimpleNamespace(st_uid=1000, st_mode=info.st_mode & ~0o022)
            return info
        with patch.object(Path, 'stat', ownership):
            with self.assertRaisesRegex(RuntimeError, 'not root controlled'):
                g.copy_sources(source, self.root / 'captured')


if __name__ == '__main__':
    unittest.main()
