# ID backup recovery

`backup.py` owns the daily cold backup of ID and YDB on `updspace-home`.
Snapshots, credentials and encrypted archives remain on the VM. This is not an
off-host backup. The older migration README describes a historical workstation
copy workflow; it is not the current storage policy.

## Incident on 2026-10-19

The 02:30 UTC snapshot reached recovery, but YDB did not become Ready within
180 seconds. The old `finally` block waited for YDB before restoring ID replicas
and CronJob schedules. Its first exception aborted every remaining recovery step.
The persisted `resume-state.json` then rejected the following daily run.

The YDB container started normally; its logs show authentication `Key not found`
errors until 08:30 UTC and readiness at 08:31. This was not an image pull failure.
The backup fix does not change authentication, credentials, the YDB version or
alert severity. It removes the dependency that stranded ID after a readiness
failure, and retries recovery when dependencies become available.

## Lifecycle

1. Take an exclusive process lock. Before stopping writers, require ready ID/YDB
   workloads and resolve/pin all current ID, YDB and CronJob images in containerd.
   Missing images abort the backup while the application is still running.
2. Atomically write and fsync the original replica counts, CronJob suspend flags
   and workload UIDs. Then suspend schedules, drain jobs and stop ID/YDB.
3. Capture sparse YDB files and ID media/metadata. Garage is read over its internal
   service so public edge cache headers do not replace original object metadata.
   Existing ID and observability credentials remain inside the encrypted bundle.
4. In `finally`, request the original replica counts for **both** YDB and ID before
   waiting. Attempt every readiness check and every CronJob restoration even if
   an earlier operation fails. Keep the marker if any operation fails.
5. After successful recovery, rename the marker to `last-resume-state.json` and
   fsync the directory. Then package/encrypt the snapshot and publish its existing
   SHA256 manifest and `COMMITTED` marker. Failed snapshots remain uncommitted.

`ExecStopPost` runs `backup.py --resume` even if the main process is killed or
hits the systemd timeout. A separate recovery timer retries each minute and after
boot. Both paths use the same lock: neither can resume an active backup. Recovery
checks saved workload UIDs and rejects replacement workloads or unexpected
replica counts. Legacy markers without UIDs remain supported for the original
incident; inspect those targets before invoking recovery manually.

The recovery timer restores desired state only; it does not take another snapshot
or erase a failed staging directory. Failed backups still need the existing
backup freshness/failure alerts. During a normal snapshot, a temporary recovery
marker is expected; `IDBackupRecoveryPending` fires only after 10 minutes.

## Install and verify

Copy `backup.py` to `/opt/updspace-id/backup.py` (root, mode 0755). Install the
backup service, existing daily timer, recovery service and recovery timer in
`/etc/systemd/system` (mode 0644). Preserve the previous code/unit on the VM.

```sh
sudo systemd-analyze verify /etc/systemd/system/updspace-id-backup*.service /etc/systemd/system/updspace-id-backup*.timer
sudo systemctl daemon-reload
sudo systemctl enable --now updspace-id-backup.timer updspace-id-backup-recover.timer
# Only recovery, no new snapshot:
sudo /opt/updspace-id/backup.py --resume
# Controlled full backup (brief ID/YDB downtime):
sudo systemctl start updspace-id-backup.service
```

Acceptance requires all of: successful backup unit; ID and YDB Ready; original
CronJob flags restored; no pending marker; newest archive hash matching both its
manifest and `COMMITTED`; and the Prometheus alert naturally resolved. Check the
notification bridge delivery state separately. A ciphertext hash is not a restore
test or confirmation that a person read Telegram.

Regression tests inject a YDB rollout timeout, a CronJob API timeout, a replaced
workload and concurrent recovery. Run `python3 -m unittest discover -s
environments/home/id-platform/id -p 'test_*.py' -v` from the repository root.

For rollback, first ensure no backup/recovery process owns the lock and complete
any pending recovery. Disable the recovery timer and restore the saved script and
backup unit together, then reload systemd. The old script has the incident's
recovery bug; reverting it is only an emergency code rollback, not remediation.
Do not delete snapshots, staging directories or a pending marker to clear alerts.
