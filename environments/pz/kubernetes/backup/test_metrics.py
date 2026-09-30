import importlib.util
import json
import math
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import mock_open, patch

spec = importlib.util.spec_from_file_location('backup_metrics', Path(__file__).with_name('metrics.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
NOW = 1790798400

class MetricsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ('state', 'upload', 'retain'): (self.root / name).mkdir()
        for name, directory in [('STATE','state'), ('UPLOAD','upload'), ('RETAIN','retain')]:
            change=patch.object(m,name,self.root/directory);change.start();self.addCleanup(change.stop)
        change=patch.object(m,'FILESYSTEMS',{});change.start();self.addCleanup(change.stop)
    def put(self,directory,name,data): (self.root/directory/name).write_text(json.dumps(data))
    def render(self,services=None): return dict(line.rsplit(' ',1) for line in m.render(NOW,services or {}).splitlines())
    def test_absent_and_corrupt_journals_are_distinct(self):
        self.assertEqual(self.render()['pz_backup_state_file_present{source="capture"}'],'0')
        path=self.root/'state/journal.json'
        for value in ('not-json','[]','{"phase":"ready"'):
            path.write_text(value)
            self.assertEqual(self.render()['pz_backup_state_file_invalid{source="capture"}'],'1')
        path.unlink();path.symlink_to(self.root/'missing')
        self.assertEqual(self.render()['pz_backup_state_file_invalid{source="capture"}'],'1')
    def test_oversized_and_fifo_inputs_rejected_without_blocking(self):
        path=self.root/'huge'
        with path.open('wb') as f:f.truncate(m.MAX_JSON+1)
        self.assertEqual(m.read(path)[1],'invalid')
        fifo=self.root/'fifo';os.mkfifo(fifo);self.assertEqual(m.read(fifo)[1],'invalid')
    def test_publish_failure_preserves_last_good_age_and_unknown_counts(self):
        self.put('upload','last-success.json',{'format':'pz-upload-receipt-v1','commit':{'captured_at':'2026-09-30T19:00:00Z','payload':{'size':123}},'upload_seconds':12.5})
        self.put('upload','remote-status.json',{'format':'pz-backup-remote-status-v1','observed_at':'2026-09-30T19:30:00Z','verification_failed':True,'verification_failure_class':'checksum'})
        data=self.render();self.assertEqual(data['pz_backup_age_seconds'],'3600.0');self.assertEqual(data['pz_backup_payload_bytes'],'123')
        self.assertEqual(data['pz_backup_failure_kind{role="upload",kind="checksum"}'],'1')
        self.assertNotIn('pz_backup_verified_snapshots{role="upload"}',data)
        self.assertNotIn('pz_backup_incomplete_multipart{role="upload"}',data)
    def test_verified_count_only_after_completed_observation(self):
        self.assertNotIn('pz_backup_verified_snapshots{role="retain"}',self.render())
        self.put('retain','remote-status.json',{'format':'pz-backup-remote-status-v1','observed_at':'2026-09-30T19:30:00Z','verification_failed':False,'verified_snapshots':5})
        self.assertEqual(self.render()['pz_backup_verified_snapshots{role="retain"}'],'5')
    def test_timings_require_both_persisted_endpoints(self):
        self.put('state','journal.json',{'phase':'ready','downtime_started_at':'2026-09-30T18:00:00Z','writers_stopped_at':'2026-09-30T18:01:00Z','staging_started_at':'2026-09-30T18:02:00Z','staging_verified_at':'2026-09-30T18:05:00Z','downtime_finished_at':'2026-09-30T18:06:00Z'})
        data=self.render();self.assertEqual(data['pz_backup_stop_seconds'],'60.0');self.assertEqual(data['pz_backup_copy_seconds'],'180.0');self.assertEqual(data['pz_backup_downtime_seconds'],'360.0');self.assertNotIn('pz_backup_archive_seconds',data)
    def test_mount_loss_does_not_report_root_capacity_as_spool(self):
        with patch.object(m,'FILESYSTEMS',{'spool':self.root}),patch.object(m,'host_mount_devices',return_value={}),patch.object(m.os,'statvfs') as statvfs:
            data=self.render();self.assertEqual(data['pz_backup_mounted{filesystem="spool"}'],'0');self.assertNotIn('pz_backup_free_bytes{filesystem="spool"}',data);statvfs.assert_not_called()
    def test_free_blocks_and_inodes_include_root(self):
        space=SimpleNamespace(f_bavail=20,f_frsize=4096,f_blocks=100,f_favail=7,f_files=10)
        with patch.object(m,'FILESYSTEMS',{'root':Path('/')}),patch.object(m,'host_mounted',return_value=True),patch.object(m.os,'statvfs',return_value=space):
            data=self.render();self.assertEqual(data['pz_backup_free_bytes{filesystem="root"}'],str(20*4096));self.assertEqual(data['pz_backup_free_inodes{filesystem="root"}'],'7')
    def test_host_mountinfo_excludes_private_service_binds(self):
        raw='32 1 8:1 / / rw - ext4 /dev/vda1 rw\n33 32 8:2 / /srv/pz-backup-spool rw - ext4 /dev/vdb rw\n'
        with patch('builtins.open',mock_open(read_data=raw)):
            devices=m.host_mount_devices()
        self.assertEqual(devices['/srv/pz-backup-spool'],(8,2))
        with patch.object(Path,'stat',return_value=SimpleNamespace(st_dev=os.makedev(8,2))):
            self.assertTrue(m.host_mounted(Path('/srv/pz-backup-spool'),devices))
            self.assertFalse(m.host_mounted(Path('/srv/pz-storage/zomboid'),devices))
        with patch.object(Path,'stat',return_value=SimpleNamespace(st_dev=os.makedev(8,3))):
            self.assertFalse(m.host_mounted(Path('/srv/pz-backup-spool'),devices))
    def test_failed_oneshot_remains_visible_after_apps_restart(self):
        data=self.render({'pz-backup-upload.service':{'LoadState':'loaded','ActiveState':'failed','Result':'exit-code','SubState':'failed'}})
        self.assertEqual(data['pz_backup_job_failed{job="upload"}'],'1');self.assertEqual(data['pz_backup_job_active{job="upload"}'],'0')
    def test_absent_optional_unit_does_not_hide_installed_job_failure(self):
        result=SimpleNamespace(returncode=1,stdout='Id=pz-backup-upload.service\nLoadState=loaded\nActiveState=failed\nResult=exit-code\n\nId=pz-disk-migration.service\nLoadState=not-found\n')
        with patch.object(m.subprocess,'run',return_value=result):
            states=m.service_states()
        self.assertEqual(self.render(states)['pz_backup_job_failed{job="upload"}'],'1')
        self.assertEqual(self.render(states)['pz_backup_job_state_known{job="migration"}'],'0')
    def test_private_labels_or_nan_cannot_be_injected(self):
        self.put('upload','last-success.json',{'format':'pz-upload-receipt-v1','commit':{'captured_at':'2026-09-30T19:00:00Z','payload':{'size':'PRIVATE_NAME\nmalicious 1'}},'upload_seconds':math.nan})
        self.put('upload','remote-status.json',{'format':'pz-backup-remote-status-v1','observed_at':'2026-09-30T19:30:00Z','verification_failed':True,'verification_failure_class':'PRIVATE_NAME'})
        text=m.render(NOW,{});self.assertNotIn('PRIVATE_NAME',text);self.assertNotIn('nan',text);self.assertNotIn('pz_backup_payload_bytes',text)
    def test_offline_restore_cannot_claim_full_attestation(self):
        self.put('retain','restore-attestation.json',{'format':'pz-backup-restore-attestation-v1','restored_at':'2026-09-30T19:00:00Z','checks':{'archive_verified':True,'isolated_runtime':True,'rcon_health':True,'world_loaded':False}})
        self.assertEqual(self.render()['pz_backup_restore_attested'],'0')
        self.put('state','restore-status.json',{'format':'pz-backup-restore-status-v1','observed_at':'2026-09-30T19:00:00Z','passed':False})
        self.assertEqual(self.render()['pz_backup_restore_failed'],'1')

if __name__=='__main__':unittest.main()
