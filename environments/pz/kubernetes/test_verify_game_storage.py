import importlib.util
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('verify_game', Path(__file__).with_name('verify-game.py'))
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)


class SharedEdgeStorageTests(unittest.TestCase):
    def test_official_panel_remains_digest_pinned_with_local_import(self):
        verifier = verify.Verifier.__new__(verify.Verifier)
        verifier.args = SimpleNamespace(expected_node='local', expected_panel_image_id=None)
        verifier.report = {'pods': {}}
        image = 'ghcr.io/fpsacha/zomboid-panel:1.4.2@sha256:' + 'a' * 64
        container = {'name': 'panel', 'image': image, 'imagePullPolicy': 'Never'}
        pod = {'metadata': {'name': 'panel', 'uid': 'test'},
               'spec': {'nodeName': 'local', 'containers': [container]},
               'status': {'containerStatuses': [{'name': 'panel', 'ready': True,
                                                  'imageID': 'sha256:' + 'a' * 64}]}}
        with patch.object(verify, 'EXPECTED_MOUNTS', {'panel': {}}):
            for policy in ('Never', 'IfNotPresent'):
                container['imagePullPolicy'] = policy
                verifier.check_pods({'panel': pod})
            container['image'] = 'ghcr.io/fpsacha/zomboid-panel:1.4.2'
            with self.assertRaises(verify.VerificationError):
                verifier.check_pods({'panel': pod})

    def test_requires_exact_volume_path_and_mounted_filesystem(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'caddy-data').mkdir()
            volume = {'spec': {'persistentVolumeReclaimPolicy': 'Retain',
                              'claimRef': {'namespace': 'edge', 'name': 'caddy-data'},
                              'local': {'path': str(root / 'caddy-data')}}}
            claim = {'status': {'phase': 'Bound'}, 'spec': {'volumeName': 'pz-caddy-data'}}
            verifier = verify.Verifier.__new__(verify.Verifier)
            verifier.args = SimpleNamespace(edge_storage_root=directory)
            verifier.report = {'persistent_volumes': {}}
            verifier.get = lambda label, args: claim if args[0] == 'pvc' else volume
            with patch.object(verify, 'EXPECTED_MOUNTS', {'caddy': {'caddy-data': ('/data', False)}}):
                with patch.object(verify.os.path, 'ismount', return_value=True):
                    verifier.check_volumes()
                    self.assertTrue(verifier.report['persistent_volumes']['caddy-data']['retained'])
                    volume['spec']['local']['path'] = '/wrong/path'
                    with self.assertRaises(verify.VerificationError):
                        verifier.check_volumes()
                volume['spec']['local']['path'] = str(root / 'caddy-data')
                with patch.object(verify.os.path, 'ismount', return_value=False):
                    with self.assertRaises(verify.VerificationError):
                        verifier.check_volumes()


if __name__ == '__main__':
    unittest.main()
