#!/usr/bin/env bash
set -euo pipefail
umask 077

# Install only the public recipient on the VM after Portal backup export approval.
backup_dir=${1:?Specify a completed PostgreSQL backup directory}
test -f "$backup_dir/COMMITTED"
(
  cd "$backup_dir"
  sha256sum -c SHA256SUMS
)
if test -e "$backup_dir/postgres.tar.gpg"; then
  (cd "$backup_dir" && sha256sum -c postgres.tar.gpg.sha256)
  exit 0
fi
tar -C "$backup_dir" -cf - updspace.dump roles.sql contents.txt SHA256SUMS | \
  gpg --batch --no-options --homedir /opt/updspace-data/gpg \
    --recipient-file /opt/updspace-data/backup-recipient.asc \
    --output "$backup_dir/postgres.tar.gpg" --encrypt
(
  cd "$backup_dir"
  sha256sum postgres.tar.gpg > postgres.tar.gpg.sha256
)
