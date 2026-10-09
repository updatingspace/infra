import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

from verify_image_archive import verify


class ImageArchiveTests(unittest.TestCase):
    def test_complete_missing_and_corrupt_layers(self):
        layer = b'example container layer'
        config = b'{"architecture":"amd64","os":"linux"}'
        sha = lambda data: 'sha256:' + hashlib.sha256(data).hexdigest()
        manifest = json.dumps({'schemaVersion': 2, 'config': {'digest': sha(config)},
                              'layers': [{'digest': sha(layer)}]}).encode()
        image = 'docker.io/example/id@' + sha(manifest)
        index = json.dumps({'schemaVersion': 2, 'manifests': [{'digest': sha(manifest),
            'annotations': {'io.containerd.image.name': image}}]}).encode()
        for mode in ('complete', 'missing', 'corrupt'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                path = Path(directory)/'images.tar'
                blobs = [('index.json', index), ('blobs/sha256/' + sha(config)[7:], config),
                         ('blobs/sha256/' + sha(manifest)[7:], manifest)]
                if mode != 'missing':
                    blobs.append(('blobs/sha256/' + sha(layer)[7:], layer if mode == 'complete' else b'damaged'))
                with tarfile.open(path, 'w') as archive:
                    for name, value in blobs:
                        info = tarfile.TarInfo(name)
                        info.size = len(value)
                        archive.addfile(info, io.BytesIO(value))
                if mode == 'complete':
                    self.assertEqual(verify(path, [image])['verified_images'], 1)
                    with self.assertRaises(ValueError):
                        verify(path, ['docker.io/example/other@' + sha(manifest)])
                else:
                    with self.assertRaises(ValueError):
                        verify(path, [image])


if __name__ == '__main__':
    unittest.main()
