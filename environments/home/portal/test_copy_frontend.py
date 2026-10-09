import hashlib
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('copy_frontend', Path(__file__).with_name('copy-frontend.py'))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class FrontendCopyTests(unittest.TestCase):
    def test_rejects_object_paths_outside_release(self):
        for key in ('../key', '/absolute', 'assets/../x', 'assets//x', './index.html'):
            with self.assertRaises(ValueError): module.relative_key(key)
        self.assertEqual(str(module.relative_key('assets/chunk.js')), 'assets/chunk.js')

    def test_validates_origin_bytes_before_installing(self):
        data = b'synthetic frontend'
        entry = {'size': len(data), 'etag': '"' + hashlib.md5(data).hexdigest() + '"'}
        self.assertEqual(module.verify(data, entry), hashlib.sha256(data).hexdigest())
        with self.assertRaises(ValueError): module.verify(b'wrong response', entry)


if __name__ == '__main__': unittest.main()
