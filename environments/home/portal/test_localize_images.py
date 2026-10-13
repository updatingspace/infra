import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('localize', Path(__file__).with_name('localize-images.py'))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class LocalImageTests(unittest.TestCase):
    def archive(self, directory, corrupt=False):
        layer = b'application files remain unchanged'
        config = module.encode({'config': {'Env': ['PORT=8000'], 'Cmd': ['app']},
                                'rootfs': {'type': 'layers', 'diff_ids': [module.digest(layer)]}})
        manifest = module.encode({'schemaVersion': 2,
            'config': {'digest': module.digest(config), 'size': len(config)},
            'layers': [{'digest': module.digest(layer), 'size': len(layer)}]})
        descriptor = {'mediaType': 'application/vnd.docker.distribution.manifest.v2+json',
                      'digest': module.digest(manifest), 'size': len(manifest)}
        blobs = {module.blob_path(module.digest(config)): config,
                 module.blob_path(module.digest(manifest)): manifest,
                 module.blob_path(module.digest(layer)): b'corrupt' if corrupt else layer,
                 'index.json': module.encode({'manifests': [descriptor]}),
                 'oci-layout': b'{"imageLayoutVersion":"1.0.0"}'}
        source = directory / 'source.tar'
        with tarfile.open(source, 'w') as tar:
            for name, data in blobs.items():
                member = tarfile.TarInfo(name)
                member.size = len(data)
                tar.addfile(member, io.BytesIO(data))
        images = {str(i): f'cloud/portal-{i}@{module.digest(manifest)}' for i in range(8)}
        return source, images, layer

    def test_only_provenance_label_changes_and_layers_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, images, layer = self.archive(root)
            before = source.read_bytes()
            result = module.localize(source, root / 'local.tar', images)
            self.assertEqual(source.read_bytes(), before)
            self.assertEqual(len(set(result.values())), 8)
            with tarfile.open(root / 'local.tar') as tar:
                self.assertEqual(tar.extractfile(module.blob_path(module.digest(layer))).read(), layer)
                for name, reference in result.items():
                    manifest = json.load(tar.extractfile(module.blob_path(reference.split('@')[1])))
                    config = json.load(tar.extractfile(module.blob_path(manifest['config']['digest'])))
                    self.assertEqual(config['config']['Labels'], {'com.updspace.source-image': images[name]})
                    self.assertEqual(config['config']['Cmd'], ['app'])

    def test_corrupt_layer_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, images, _ = self.archive(root, corrupt=True)
            with self.assertRaises(AssertionError):
                module.localize(source, root / 'local.tar', images)


if __name__ == '__main__':
    unittest.main()
