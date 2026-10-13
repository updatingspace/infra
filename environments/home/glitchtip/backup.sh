#!/usr/bin/env bash
set -euo pipefail
umask 077

# Pause this single replica so PostgreSQL metadata and uploaded files agree.
exec 9>/run/lock/glitchtip-backup.lock
flock -n 9
backup_root=/srv/updspace/backups/glitchtip
mkdir -p "$backup_root"
backup_dir=$(mktemp -d "$backup_root/$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX")
replicas=$(k3s kubectl -n glitchtip get deployment glitchtip -o jsonpath='{.spec.replicas}')
case "$replicas" in 0|1) ;; *) echo 'Unexpected GlitchTip replicas; refusing backup' >&2; exit 1 ;; esac
resume=false
restore_replica() {
  if test "$resume" = true; then
    k3s kubectl -n glitchtip scale deployment glitchtip --current-replicas=0 --replicas=1
  fi
}
trap restore_replica EXIT
if test "$replicas" = 1; then
  k3s kubectl -n glitchtip scale deployment glitchtip --current-replicas=1 --replicas=0
  resume=true
fi
k3s kubectl -n glitchtip wait --for=delete pod -l app.kubernetes.io/name=glitchtip --timeout=90s
k3s kubectl -n updspace-data exec postgres-0 -- \
  pg_dump -U postgres -d glitchtip -Fc > "$backup_dir/glitchtip.dump"
test -s "$backup_dir/glitchtip.dump"
k3s kubectl -n updspace-data exec -i postgres-0 -- \
  pg_restore --list < "$backup_dir/glitchtip.dump" > "$backup_dir/contents.txt"
(
  cd "$backup_dir"
  sha256sum glitchtip.dump contents.txt > SHA256SUMS
  sha256sum -c SHA256SUMS
)
tar -cf - -C "$backup_dir" glitchtip.dump contents.txt SHA256SUMS \
  -C /srv/glitchtip uploads \
  -C /opt/updspace-infra/private glitchtip-credentials.json | \
  gpg --batch --no-options --homedir /opt/updspace-data/gpg \
    --recipient-file /opt/updspace-data/backup-recipient.asc \
    --output "$backup_dir/postgres.tar.gpg" --encrypt
(
  cd "$backup_dir"
  sha256sum postgres.tar.gpg > postgres.tar.gpg.sha256
  sha256sum -c postgres.tar.gpg.sha256
)
touch "$backup_dir/COMMITTED"
printf '%s\n' "$backup_dir"
