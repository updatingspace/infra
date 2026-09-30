"""Real age/zstd/tar integration; uses an ephemeral fixture key, never production."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import coordinator
import restore


@unittest.skipUnless(all(shutil.which(x) for x in ('age','age-keygen','zstd','tar')), 'archive tools required')
class ArchiveRoundtrip(unittest.TestCase):
    def test_encrypted_archive_restores_exact_bytes_and_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);stage=root/'stage';(stage/'data').mkdir(parents=True);(stage/'recovery').mkdir()
            (stage/'data/world.db').write_bytes(b'fixture-world\x00' * 1000)
            (stage/'recovery/index.json').write_text('{"complete":true}\n')
            records=[]
            for prefix in ('data','recovery'):
                for item in coordinator.inventory(stage/prefix):
                    records.append({**item,'path':prefix if item['path']=='.' else prefix+'/'+item['path']})
            manifest={'format':'pz-backup-v1','snapshot_id':'20260930T010203Z-'+'a'*32,'captured_at':'2026-09-30T01:02:03Z','files':records}
            encoded=(json.dumps(manifest,sort_keys=True,separators=(',',':'))+'\n').encode();(stage/'manifest.json').write_bytes(encoded)
            key=root/'fixture.key';subprocess.run(['age-keygen','-o',str(key)],capture_output=True,check=True)
            recipient=subprocess.check_output(['age-keygen','-y',str(key)],text=True).strip()
            result=coordinator.encrypted_archive(stage,stage/'payload.enc',manifest,recipient)
            self.assertEqual(result['sha256'],coordinator.file_hash(stage/'payload.enc'))
            age=subprocess.Popen(['age','-d','-i',str(key),str(stage/'payload.enc')],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            decompressor=subprocess.Popen(['zstd','-dq'],stdin=age.stdout,stdout=subprocess.PIPE,stderr=subprocess.PIPE);age.stdout.close()
            try:
                report=restore.extract_verified(decompressor.stdout,manifest,encoded,root/'restored')
                while decompressor.stdout.read(1024*1024): pass
                self.assertEqual(decompressor.wait(timeout=20),0);self.assertEqual(age.wait(timeout=20),0)
                self.assertTrue(report['archive_verified'])
                self.assertEqual((root/'restored/data/world.db').read_bytes(),(stage/'data/world.db').read_bytes())
            finally:
                for process in (decompressor,age):
                    if process.poll() is None:process.kill();process.wait()
                    for handle in (process.stdout,process.stderr):
                        if handle and not handle.closed:handle.close()
