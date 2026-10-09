#!/usr/bin/env bash
set -euo pipefail
umask 077

# Run on updspace-home as root; a backup is complete only after COMMITTED exists.
backup_root=/srv/updspace/backups/postgres
mkdir -p "$backup_root"
backup_dir=$(mktemp -d "$backup_root/$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX")
k3s kubectl -n updspace-data exec postgres-0 -- \
  pg_dump -U postgres -d updspace -Fc > "$backup_dir/updspace.dump"
k3s kubectl -n updspace-data exec postgres-0 -- \
  pg_dumpall -U postgres --roles-only --no-role-passwords > "$backup_dir/roles.sql"
test -s "$backup_dir/updspace.dump"
test -s "$backup_dir/roles.sql"
k3s kubectl -n updspace-data exec -i postgres-0 -- \
  pg_restore --list < "$backup_dir/updspace.dump" > "$backup_dir/contents.txt"
(
  cd "$backup_dir"
  sha256sum updspace.dump roles.sql contents.txt > SHA256SUMS
  sha256sum -c SHA256SUMS
)
touch "$backup_dir/COMMITTED"
bash /opt/updspace-data/encrypt-postgres-backup.sh "$backup_dir"
printf '%s\n' "$backup_dir"
