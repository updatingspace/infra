"""Two-pass staging fixtures without production data, cluster or credentials."""
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch
import coordinator as c
import restore


class TwoPass(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data = self.root/'zomboid'
        self.data.mkdir()
        self.stage = self.root/'stage'
        self.stage.mkdir()
        (self.stage/'recovery').mkdir()
        (self.stage/'recovery/index.json').write_text('{"complete":true}\n')
        self.file = self.data/'a-world.db'
        self.content = b'known-source-world-bytes\0' * 100000
        self.file.write_bytes(self.content)
        self.file.chmod(0o640)
        os.setxattr(self.file, 'user.binary', b'\0\xff\x80with\nnewline=')
        os.utime(self.file, ns=(1234567890123456789, 1234567890123456789))
        os.link(self.file, self.data/'b-hardlink')
        (self.data/'c-link').symlink_to('a-world.db')
        os.utime(self.data/'c-link', ns=(1000000000123456789,1000000000123456789), follow_symlinks=False)
        self.cfg = {'max_entries':100, 'max_snapshot_bytes':20*1024*1024,
                    'server_name':'fixture', 'infra_revision':'b'*40}

    def tearDown(self): self.tmp.cleanup()

    def fixture(self, known_hashes=False):
        identities, records = {}, []
        for prefix, root in [('data',self.data), ('recovery',self.stage/'recovery')]:
            identities[prefix] = {}
            for row in c.inventory(root, hashes=known_hashes, identities=identities[prefix]):
                row['path'] = prefix if row['path']=='.' else prefix+'/'+row['path']
                if 'hardlink' in row: row['hardlink'] = prefix+'/'+row['hardlink']
                records.append(row)
        manifest = {'format':c.FORMAT, 'snapshot_id':'20261001T000000Z-'+'a'*32,
                    'captured_at':'2026-10-01T00:00:00Z', 'excluded':[], 'files':records}
        return manifest, identities

    def create(self, manifest, ids):
        with patch.object(c,'DATA',self.data):
            return c.create_two_pass_staging_tar(self.stage,manifest,time.monotonic()+30,ids['data'],ids['recovery'])

    def acl(self):
        # Linux POSIX ACL: named UID makes it nontrivial (not reducible to mode).
        acl = struct.pack('<I',2) + b''.join(struct.pack('<HHI',*entry) for entry in
            [(1,6,0xffffffff),(2,4,os.getuid()),(4,4,0xffffffff),(16,4,0xffffffff),(32,0,0xffffffff)])
        os.setxattr(self.file,'system.posix_acl_access',acl)
        os.setxattr(self.data,'system.posix_acl_default',acl)
        return acl

    def test_full_metadata_links_binary_xattrs_acl_and_manifest_last(self):
        acl = self.acl()
        long = self.data/('длинное-имя-'+'x'*110)
        long.write_bytes(b'long pax path')
        os.utime(long,ns=(-123456789,-123456789))
        manifest, ids = self.fixture()
        result = self.create(manifest, ids)
        encoded = (self.stage/'manifest.json').read_bytes()
        with tarfile.open(self.stage/'staging.tar') as archive:
            entries = list(archive)
        self.assertEqual(entries[-1].name,'manifest.json')
        self.assertEqual(result['sha256'],c.file_hash(self.stage/'staging.tar'))
        self.assertEqual(c.read_json(self.stage/'manifest.json'),manifest)
        self.assertEqual(next(row for row in manifest['files'] if row['path']=='data/a-world.db')['sha256'],hashlib.sha256(self.content).hexdigest())
        for row in manifest['files']:
            self.assertFalse(set(row)&{'ino','ctime_ns','dev','identities'})
        with (self.stage/'staging.tar').open('rb') as stream:
            report=restore.extract_verified(stream,manifest,encoded,self.root/'restored')
        self.assertTrue(report['archive_verified'])
        self.assertEqual(os.getxattr(self.root/'restored/data/a-world.db','system.posix_acl_access'),acl)
        self.assertEqual(os.getxattr(self.root/'restored/data','system.posix_acl_default'),acl)
        self.assertEqual((self.root/'restored/data/a-world.db').stat().st_ino,(self.root/'restored/data/b-hardlink').stat().st_ino)
        self.assertEqual((self.root/'restored/data'/long.name).stat().st_mtime_ns,-123456789)
        self.assertEqual(os.readlink(self.root/'restored/data/c-link'),'a-world.db')
        if not shutil.which('tar'): self.fail('GNU tar required for compatibility proof')
        target=self.root/'gnu';target.mkdir()
        subprocess.run(['tar','--acls','--xattrs','--xattrs-include=*','-xpf',str(self.stage/'staging.tar'),'-C',str(target)],check=True,capture_output=True)
        self.assertEqual(c.inventory(target/'data'),c.inventory(self.data))

    def test_real_age_zstd_roundtrip_after_live_source_resumed(self):
        if not all(shutil.which(tool) for tool in ('age','age-keygen','zstd')):
            self.fail('real age/zstd tools required; do not count this test as skipped')
        manifest,ids=self.fixture()
        staged=self.create(manifest,ids)
        encoded=(self.stage/'manifest.json').read_bytes()
        self.file.write_bytes(b'production resumed; staging must stay independent')
        key=self.root/'fixture.key'
        subprocess.run(['age-keygen','-o',str(key)],check=True,capture_output=True)
        recipient=subprocess.check_output(['age-keygen','-y',str(key)],text=True).strip()
        c.encrypted_archive(self.stage,self.stage/'payload.enc',manifest,recipient,staged_tar=staged)
        encrypted=subprocess.check_output(['age','-d','-i',str(key),str(self.stage/'payload.enc')])
        plain=subprocess.check_output(['zstd','-dq'],input=encrypted)
        report=restore.extract_verified(io.BytesIO(plain),manifest,encoded,self.root/'roundtrip')
        self.assertTrue(report['archive_verified'])
        self.assertEqual((self.root/'roundtrip/data/a-world.db').read_bytes(),self.content)

    def test_punctuation_sibling_hardlink_before_dfs_target_is_deferred(self):
        (self.data/'a').mkdir()
        target=self.data/'a/world'
        target.write_bytes(b'world selected first by DFS')
        os.setxattr(target,'user.binary',b'\0\xff')
        os.link(target,self.data/'a.world')
        manifest,ids=self.fixture()
        paths=[row['path'] for row in manifest['files']]
        self.assertLess(paths.index('data/a.world'),paths.index('data/a/world'))
        self.assertEqual(next(row for row in manifest['files'] if row['path']=='data/a.world')['hardlink'],'data/a/world')
        original_paths=list(paths)
        self.create(manifest,ids)
        self.assertEqual([row['path'] for row in manifest['files']],original_paths)
        with tarfile.open(self.stage/'staging.tar') as archive:
            written=[entry.name for entry in archive]
        self.assertLess(written.index('data/a/world'),written.index('data/a.world'))
        encoded=(self.stage/'manifest.json').read_bytes()
        with (self.stage/'staging.tar').open('rb') as stream:
            report=restore.extract_verified(stream,manifest,encoded,self.root/'punctuation')
        self.assertTrue(report['archive_verified'])
        a=self.root/'punctuation/data/a/world';b=self.root/'punctuation/data/a.world'
        self.assertEqual(a.read_bytes(),b'world selected first by DFS')
        self.assertEqual(a.stat().st_ino,b.stat().st_ino)
        self.assertEqual(os.getxattr(b,'user.binary'),b'\0\xff')

    def test_precomputed_known_sha_mismatch_never_overwritten(self):
        manifest, ids = self.fixture(known_hashes=True)
        original = next(row for row in manifest['files'] if row['path']=='data/a-world.db')['sha256']
        self.file.write_bytes(b'Z'*len(self.content))
        ids['data']={}
        c.inventory(self.data,hashes=False,identities=ids['data'])
        with self.assertRaisesRegex(c.Refused,'source_known_sha256_mismatch'): self.create(manifest,ids)
        self.assertEqual(next(row for row in manifest['files'] if row['path']=='data/a-world.db')['sha256'],original)
        self.assertFalse((self.stage/'staging.tar').exists())

    def test_source_changes_while_fd_is_read_are_refused(self):
        manifest,ids=self.fixture()
        original=c.SourceHashReader.read
        changed=False
        def read(reader,count=-1):
            nonlocal changed
            data=original(reader,count)
            if not changed:
                changed=True
                with self.file.open('r+b') as target: target.write(b'MUTATED')
            return data
        with patch.object(c.SourceHashReader,'read',read):
            with self.assertRaisesRegex(c.Refused,'source_changed_while_copying'):self.create(manifest,ids)
        self.assertFalse((self.stage/'staging.tar').exists())

    def test_path_replacement_even_with_same_bytes_and_mtime_is_refused(self):
        manifest,ids=self.fixture()
        clone=self.data/'replacement';clone.write_bytes(self.content)
        info=self.file.stat();os.chmod(clone,info.st_mode);os.utime(clone,ns=(info.st_atime_ns,info.st_mtime_ns))
        os.replace(clone,self.file)
        with self.assertRaisesRegex(c.Refused,'source_changed_before_copy'):self.create(manifest,ids)

    def test_ancestor_symlink_cannot_redirect_source(self):
        child=self.data/'dir';child.mkdir();(child/'content').write_bytes(b'bytes')
        manifest,ids=self.fixture()
        elsewhere=self.root/'elsewhere';child.rename(elsewhere);child.symlink_to(elsewhere)
        with self.assertRaises((c.Refused,OSError)):self.create(manifest,ids)
        self.assertFalse((self.stage/'staging.tar').exists())

    def test_disk_corruption_is_detected_by_independent_readback(self):
        manifest,ids=self.fixture()
        original=c.verify_tar
        def corrupt_then_verify(stream,*args):
            with (self.stage/'staging.tar.partial').open('r+b') as target:
                data=target.read();offset=data.index(b'known-source-world-bytes')
                target.seek(offset);target.write(b'CORRUPTED')
            return original(stream,*args)
        with patch.object(c,'verify_tar',side_effect=corrupt_then_verify):
            with self.assertRaisesRegex(c.Refused,'archive_sha256_mismatch'):self.create(manifest,ids)
        self.assertFalse((self.stage/'staging.tar').exists())

    def test_final_source_postcheck_still_refuses_late_extra_file(self):
        coordinator=c.Coordinator(self.cfg,object());coordinator.journal_path=self.root/'journal.json'
        coordinator.journal={'snapshot_id':'20261001T000000Z-'+'a'*32,'original':{}}
        original=c.verify_tar
        def mutate_after_copy(*args,**kwargs):
            original(*args,**kwargs)
            (self.data/'late-file').write_bytes(b'writer ran')
        with patch.object(c,'DATA',self.data),patch.object(c,'free_space'),patch.object(c,'verify_tar',side_effect=mutate_after_copy):
            with self.assertRaisesRegex(c.Refused,'source_changed_during_staging'):
                coordinator.stage_stopped(self.stage,{'already_stopped':True})
        self.assertEqual(coordinator.journal['phase'],'staging')
        self.assertNotIn('staging_verified_at',coordinator.journal)

    def test_content_read_once_per_distinct_source_inode(self):
        manifest,ids=self.fixture()
        original=c.SourceHashReader.read;counts=[]
        def read(reader,count=-1):
            result=original(reader,count);counts.append(len(result));return result
        with patch.object(c.SourceHashReader,'read',read):self.create(manifest,ids)
        self.assertEqual(sum(counts),len(self.content)+len((self.stage/'recovery/index.json').read_bytes()))

    def test_deadline_cannot_be_reset_during_source_copy(self):
        manifest,ids=self.fixture()
        original=c.SourceHashReader.read
        def read(reader,count=-1):
            reader.deadline=time.monotonic()-1
            return original(reader,count)
        with patch.object(c.SourceHashReader,'read',read):
            with self.assertRaisesRegex(c.Refused,'staging_deadline_exceeded'):self.create(manifest,ids)
        self.assertFalse((self.stage/'staging.tar').exists())
        self.assertFalse((self.stage/'manifest.json').exists())

    def test_future_manifest_limit_includes_all_later_sha_fields(self):
        manifest,_=self.fixture()
        size=len((json.dumps(manifest,sort_keys=True,separators=(',',':'))+'\n').encode())
        with patch.object(c,'MANIFEST_MAX_BYTES',size):
            with self.assertRaisesRegex(c.Refused,'manifest_size_limit_exceeded'):
                c.check_future_manifest_size(manifest,time.monotonic()+30)


if __name__=='__main__':unittest.main()
