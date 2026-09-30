"""Real age/zstd/tar integration; uses an ephemeral fixture key, never production."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
import coordinator
import restore


@unittest.skipUnless(all(shutil.which(x) for x in ('age', 'age-keygen', 'zstd', 'tar')), 'archive tools required')
class ArchiveRoundtrip(unittest.TestCase):
    def fixture(self, root, tar_staging):
        stage = root / 'stage'
        stage.mkdir()
        source = root / 'zomboid' if tar_staging else stage / 'data'
        source.mkdir()
        (stage / 'recovery').mkdir()
        (source / 'world.db').write_bytes(b'fixture-world\x00' * 1000)
        os.setxattr(source / 'world.db', 'user.binary', b'\0\xfffixture')
        os.link(source / 'world.db', source / 'world.hardlink')
        (source / 'relative').symlink_to('world.db')
        (stage / 'recovery/index.json').write_text('{"complete":true}\n')
        records = []
        for prefix, path in [('data', source), ('recovery', stage / 'recovery')]:
            for item in coordinator.inventory(path):
                row = {**item, 'path': prefix if item['path'] == '.' else prefix + '/' + item['path']}
                if 'hardlink' in row:
                    row['hardlink'] = prefix + '/' + row['hardlink']
                records.append(row)
        manifest = {'format': 'pz-backup-v1', 'snapshot_id': '20260930T010203Z-' + 'a' * 32,
                    'captured_at': '2026-09-30T01:02:03Z', 'excluded': [], 'files': records}
        encoded = (json.dumps(manifest, sort_keys=True, separators=(',', ':')) + '\n').encode()
        (stage / 'manifest.json').write_bytes(encoded)
        staged_tar = None
        if tar_staging:
            with patch.object(coordinator, 'DATA', source):
                staged_tar = coordinator.create_staging_tar(stage, manifest, time.monotonic() + 30)
        return stage, source, manifest, encoded, staged_tar

    def test_legacy_and_tar_staging_restore_exact_bytes_metadata_and_hardlinks(self):
        for tar_staging in [False, True]:
            with self.subTest(tar_staging=tar_staging), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                stage, source, manifest, encoded, staged_tar = self.fixture(root, tar_staging)
                if tar_staging:
                    # Live data may change once applications resume. Encryption
                    # must consume only the already verified immutable TAR.
                    (source / 'world.db').write_bytes(b'live server resumed')
                key = root / 'fixture.key'
                subprocess.run(['age-keygen', '-o', str(key)], capture_output=True, check=True)
                recipient = subprocess.check_output(['age-keygen', '-y', str(key)], text=True).strip()
                result = coordinator.encrypted_archive(stage, stage / 'payload.enc', manifest, recipient, staged_tar=staged_tar)
                self.assertEqual(result['sha256'], coordinator.file_hash(stage / 'payload.enc'))
                if tar_staging:
                    self.assertEqual(result['plaintext_sha256'], staged_tar['sha256'])
                    self.assertEqual(result['plaintext_size'], staged_tar['size'])
                age = subprocess.Popen(['age', '-d', '-i', str(key), str(stage / 'payload.enc')], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                decompressor = subprocess.Popen(['zstd', '-dq'], stdin=age.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                age.stdout.close()
                try:
                    report = restore.extract_verified(decompressor.stdout, manifest, encoded, root / 'restored')
                    while decompressor.stdout.read(1024 * 1024):
                        pass
                    self.assertEqual(decompressor.wait(timeout=20), 0)
                    self.assertEqual(age.wait(timeout=20), 0)
                    self.assertTrue(report['archive_verified'])
                    restored = root / 'restored/data'
                    self.assertEqual((restored / 'world.db').read_bytes(), b'fixture-world\x00' * 1000)
                    self.assertEqual(os.getxattr(restored / 'world.db', 'user.binary'), b'\0\xfffixture')
                    self.assertEqual((restored / 'world.db').stat().st_ino, (restored / 'world.hardlink').stat().st_ino)
                    self.assertEqual(os.readlink(restored / 'relative'), 'world.db')
                finally:
                    for process in (decompressor, age):
                        if process.poll() is None:
                            process.kill()
                            process.wait()
                        for handle in (process.stdout, process.stderr):
                            if handle and not handle.closed:
                                handle.close()

    def test_encryption_failure_reaps_all_children_and_keeps_plaintext_for_inspection(self):
        with tempfile.TemporaryDirectory() as tmp:
            stage, source, manifest, encoded, staged_tar = self.fixture(Path(tmp), True)
            processes = []
            real_popen = subprocess.Popen
            def track(*args, **kwargs):
                process = real_popen(*args, **kwargs)
                processes.append(process)
                return process
            with patch.object(coordinator.subprocess, 'Popen', side_effect=track):
                with self.assertRaises((coordinator.Refused, BrokenPipeError)):
                    coordinator.encrypted_archive(stage, stage / 'payload.enc', manifest, 'invalid-recipient', staged_tar=staged_tar)
            self.assertTrue(processes)
            self.assertTrue(all(process.poll() is not None for process in processes))
            self.assertFalse((stage / 'payload.enc').exists())
            self.assertTrue((stage / 'staging.tar').exists())

    def test_source_mutation_during_tar_creation_cannot_become_verified_staging(self):
        with tempfile.TemporaryDirectory() as tmp:
            stage, source, manifest, encoded, _ = self.fixture(Path(tmp), False)
            target = Path(tmp) / 'zomboid'
            source.rename(target)
            (target / 'world.db').write_bytes(b'different!!!!\x00' * 1000)
            with patch.object(coordinator, 'DATA', target):
                with self.assertRaises(coordinator.Refused):
                    coordinator.create_staging_tar(stage, manifest, time.monotonic() + 30)
            self.assertFalse((stage / 'staging.tar').exists())
            self.assertTrue((stage / 'staging.tar.partial').exists())
