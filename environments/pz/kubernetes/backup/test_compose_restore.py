"""Exercise the actual restore composition on small, isolated trees."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest

import coordinator
import remote
import restore

SPEC = importlib.util.spec_from_file_location('compose_restore', Path(__file__).with_name('compose-restore.py'))
compose = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(compose)


class ComposeRestoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.base_root = self.root / 'base'
        self.state_root = self.root / 'state'
        for root in (self.base_root, self.state_root):
            root.mkdir(mode=0o700)
            for path in ('data/pz-server/steamapps/workshop', 'data/zomboid/Saves/Multiplayer/survival42',
                         'data/zomboid/Server', 'data/panel', 'data/steam', 'recovery'):
                (root / path).mkdir(parents=True, exist_ok=True)
        for component in coordinator.BASE_COMPONENTS:
            path = self.base_root / 'data' / component
            path.mkdir(parents=True, exist_ok=True)
            (path / 'artifact').write_bytes(component.encode())
        (self.base_root / 'data/zomboid/Saves/Multiplayer/survival42/world.bin').write_bytes(b'old world')
        (self.state_root / 'data/zomboid/Saves/Multiplayer/survival42/world.bin').write_bytes(b'new world')
        for root in (self.base_root, self.state_root):
            (root / 'data/zomboid/Server/survival42.ini').write_text('Map=Muldraugh\n')
            (root / 'data/panel/panel.db').write_bytes(b'panel state')
            (root / 'data/steam/config').write_bytes(b'steam config')
            (root / 'recovery/runtime-images.oci.tar').write_bytes(b'oci image')
        self.base_id = '20261004T030122Z-' + 'a' * 32
        self.state_id = '20261005T030122Z-' + 'b' * 32
        self.base_commit = 'c' * 64
        self.state_commit = 'd' * 64
        self.base = self.manifest(self.base_root, self.base_id)
        components = {}
        for component in coordinator.BASE_COMPONENTS:
            prefix = 'data/' + component
            rows = [row for row in self.base['files'] if row['path'] == prefix
                    or row['path'].startswith(prefix + '/')]
            components[component] = hashlib.sha256(json.dumps(rows, sort_keys=True,
                separators=(',', ':')).encode()).hexdigest()
        self.state = self.manifest(self.state_root, self.state_id)
        self.state['snapshot_profile'] = 'state-with-base-v1'
        self.state['base_snapshot'] = {'snapshot_id': self.base_id,
                                       'commit_sha256': self.base_commit, 'components': components}
        self.state['excluded'] = list(coordinator.BASE_COMPONENTS)
        self.save(self.base_root, self.base, self.base_commit)
        self.save(self.state_root, self.state, self.state_commit)

    def manifest(self, root, sid):
        rows = [row for row in coordinator.inventory(root) if row['path'] != '.']
        return {'format': 'pz-backup-v1', 'snapshot_id': sid,
                'captured_at': '2026-10-05T03:01:22Z', 'files': rows}

    def save(self, root, manifest, commit):
        remote._atomic_json(root / 'manifest.json', manifest)
        remote._atomic_json(root / 'restored.json', {'snapshot_id': manifest['snapshot_id'],
            'commit_sha256': commit, 'archive_verified': True})

    def test_current_world_and_exact_base_artifacts_compose(self):
        destination = self.root / 'composed'
        result = compose.compose(self.base_root, self.state_root, destination)
        self.assertTrue(result['composition_verified'])
        self.assertEqual((destination / 'data/zomboid/Saves/Multiplayer/survival42/world.bin').read_bytes(), b'new world')
        self.assertEqual((destination / 'data/pz-server/media/artifact').read_bytes(), b'pz-server/media')
        report = remote._private_json(destination / 'restored.json')
        self.assertEqual(report['base_commit_sha256'], self.base_commit)
        self.assertEqual(restore.validate_manifest(compose.manifest_at(destination))['data/pz-server/media/artifact']['sha256'],
                         hashlib.sha256(b'pz-server/media').hexdigest())

    def test_wrong_base_commit_refuses_before_copy(self):
        self.state['base_snapshot']['commit_sha256'] = 'e' * 64
        self.save(self.state_root, self.state, self.state_commit)
        with self.assertRaisesRegex(restore.RestoreError, 'base_restore_identity_mismatch'):
            compose.compose(self.base_root, self.state_root, self.root / 'bad')
        self.assertFalse((self.root / 'bad').exists())
