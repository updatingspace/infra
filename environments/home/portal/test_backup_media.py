import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import unittest

spec=importlib.util.spec_from_file_location('media_backup',Path(__file__).with_name('backup-media.py'))
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)

def archive(payload=b'synthetic', claimed=None, extra=False):
    buffer=io.BytesIO()
    with tarfile.open(fileobj=buffer,mode='w') as output:
        def add(name,value):
            item=tarfile.TarInfo(name);item.size=len(value);output.addfile(item,io.BytesIO(value))
        add('objects/00000000',payload)
        add('manifest.json',json.dumps({'format':1,'objects':[{'file':'objects/00000000','size':len(payload),'sha256':hashlib.sha256(payload if claimed is None else claimed).hexdigest()}]}).encode())
        if extra:add('unexpected',b'extra')
    return buffer.getvalue()

class MediaBackupTests(unittest.TestCase):
    def test_complete_body_is_verified(self):
        self.assertEqual(len(module.verify_archive(archive())['objects']),1)
    def test_corrupted_body_is_rejected(self):
        with self.assertRaises(AssertionError):module.verify_archive(archive(claimed=b'different'))
    def test_unexpected_archive_members_are_rejected(self):
        with self.assertRaises(AssertionError):module.verify_archive(archive(extra=True))

if __name__=='__main__':unittest.main()
