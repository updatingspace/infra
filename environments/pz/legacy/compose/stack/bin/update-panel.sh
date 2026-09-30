#!/usr/bin/env bash
# Intentionally targets only panel. Game image/config and volumes are untouched.
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
exec 9>.deploy.lock
flock -n 9 || { echo 'Another deployment is in progress' >&2; exit 1; }
version="${1:?Usage: bin/update-panel.sh v1.3.7}"
[[ "$version" =~ ^v[0-9]+\.[0-9]+\.[0-9]+([.-][A-Za-z0-9.-]+)?$ ]] || exit 64
identity() {
    local id
    id=$(docker compose ps -a -q zomboid)
    [[ -n "$id" ]] || { echo 'absent'; return 0; }
    docker inspect --format '{{.Id}} {{.State.StartedAt}} {{.RestartCount}}' "$id"
}
before=$(identity)
PANEL_REF="$version" docker compose build panel
PANEL_REF="$version" docker compose up -d --no-deps --no-build --pull never --wait --wait-timeout 120 panel
after=$(identity)
[[ "$before" == "$after" ]] || { echo 'ERROR: game container identity changed during deployment; investigate' >&2; exit 1; }
# Persist the chosen panel version only after health and game identity checks.
python3 - "$version" <<'PY'
from pathlib import Path
import os, re, sys, tempfile
p=Path('.env')
info=p.stat()
s=p.read_text()
line='PANEL_REF='+sys.argv[1]
s=re.sub(r'^PANEL_REF=.*$', lambda _:line, s, flags=re.M) if re.search(r'^PANEL_REF=',s,re.M) else s.rstrip()+'\n'+line+'\n'
fd, tmp=tempfile.mkstemp(prefix='.env.panel-', dir='.')
try:
    with os.fdopen(fd, 'w') as f:
        f.write(s)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, info.st_mode & 0o777)
    if os.geteuid()==0: os.chown(tmp, info.st_uid, info.st_gid)
    os.replace(tmp, p)
finally:
    if os.path.exists(tmp): os.unlink(tmp)
PY
printf 'Panel %s is healthy. Game container and start time are unchanged.\n' "$version"
