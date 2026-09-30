# Bounded storage on the existing VM

This root migrates existing data to three independent, bounded ext4 filesystems:

| Environment | Maximum image size | Mount |
|---|---:|---|
| zomboid | 26 GiB | `/srv/pz-storage/zomboid` |
| edge | 256 MiB | `/srv/pz-storage/edge` |
| observability | 512 MiB | `/srv/pz-storage/observability` |

Images live at `/var/lib/pz-volumes/<environment>.ext4`. This enforces filesystem
capacity for each environment, including persistent files excluded from normal
Kubernetes ephemeral-storage accounting. Ext4 metadata reduces usable capacity
slightly. Every filesystem remains on one physical SSD. Sparse images do not
reserve physical space; host exhaustion affects all services, so monitor host
free space and preserve an off-host backup.

The Python adapter connects over SSH with passwordless sudo. It stores the exact
script, specification, identity hashes and result in `/var/lib/pz-volumes/job`
(directory mode 0700, files 0600) and starts one detached systemd service,
`pz-storage-migration.service`. SSH and Terraform are not the migration's parent
processes. A dropped connection cannot terminate an in-progress copy. The fixed
unit name, controller lock and immutable job identity prevent duplicate launches
when the launch acknowledgment or a later status response is lost.

Before any migration the worker
requires all four `pz-b42` Compose services stopped, game exit code 0, no active
application Kubernetes pods, and a verified backup manifest at
`/var/backups/pz-k3s-20260930/verified.json`. It rechecks the archive SHA256.

```sh
terraform init
terraform validate
terraform plan -var='release_verified_sources=true' -out=storage.tfplan
terraform apply storage.tfplan
```

Terraform keeps waiting while short SSH polls reconnect every 15 seconds. Each
request is limited to 45 seconds; waiting is capped at two hours and 120
consecutive transport failures. A local timeout or loss of Terraform does not
stop the server job. Re-running the same script and specification attaches to
the existing job. Output includes only approved copy phases, directory names and
statuses; raw SSH output, service logs and migration contents are not forwarded.

Success requires all three proofs: `migration.json` contains `completed_at`, the
job result matches that timestamp and both identity hashes, and systemd reports
the process exited normally with status 0. `RemainAfterExit=yes` preserves that
unit status. Failed migrations are never restarted automatically. A reboot can
remove the transient unit; a saved result without its unit requires operator
review and is not silently reported as a confirmed success or relaunched.

For diagnosis, inspect `systemctl status pz-storage-migration.service` and the
root-only job files on the VM. Review the migration journal and any partial
retired source before explicitly resetting a failed job. Keep its script/spec
unchanged while running or reconnecting. Preserve failed job artifacts before
an operator-approved new attempt; do not delete images or the migration journal.
If the Terraform provisioner already failed, resolve its tainted resource only
after checking the persistent job; destroying storage is never a recovery step.

After a local script change, regenerate the reviewable plan with the same
`terraform plan -var='release_verified_sources=true' -out=storage.tfplan` command
before applying. Existing jobs reject different script/spec hashes.

Each source directory is copied with numeric ownership, permissions, hardlinks,
ACLs and xattrs. A second `rsync --dry-run -aHAXc --delete` must report no changes.
Each directory needs one full checksum comparison per invocation. Within that
invocation the verified source/destination device and inode identities are
checked across rename and source release, with application writers confirmed
stopped before comparison. Resumed migrations always compare again.
Only then is the old directory renamed to `<name>.pre-k3s` and replaced with a
symlink to the mounted copy. The explicit `release_verified_sources=true` input
also removes each checksum-verified, backed-up retired directory, releasing disk
space before the next copy. Without it, originals remain for operator review.
The adapter checks a 2 GiB physical host reserve before every copy.

The journal `/var/lib/pz-volumes/migration.json` records filesystem UUIDs and each
copy/verification/link/release transition. Existing images are never formatted
or resized. Failed copying can be retried; an interrupted source release stops
for inspection against the archive before manual cleanup. Do not discard the
journal or create replacement images to bypass a failure.

Managed `/etc/fstab` entries mount the images at boot. Docker and k3s systemd
drop-ins require all three mounts and check that each is a mountpoint before
starting, preventing writes to empty underlying directories. The adapter does
not restart either service. `/var/lib/pz-volumes/fstab.before-migration` preserves
the original fstab.

The legacy paths `/opt/pz-stack/data/*` remain valid symlinks for the application
PVs, monitoring and Compose rollback. Gracefully stop the Kubernetes game and
panel before starting Compose against these same paths. There is no destroy
provisioner; Terraform prevents deleting the storage adapter resource. Future
growth requires a separate reviewed filesystem-resize procedure. Terraform
does not automatically detect host filesystem drift; verify mounts with
`findmnt` and compare the migration journal before maintenance.
