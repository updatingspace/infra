#!/usr/bin/env bash
set -Eeuo pipefail
fixture=$(mktemp -d /tmp/pz-runtime-smoke.XXXXXX)
chmod 755 "$fixture"
mkdir -p "$fixture/game" "$fixture/data/Server"
chmod -R 777 "$fixture/game" "$fixture/data"
printf '{"vmArgs":["-Xms1g","-Xmx1g","-Dkeep.fixture=true"]}\n' > "$fixture/game/ProjectZomboid64.json"
printf '# existing fixture world\n' > "$fixture/data/Server/survival42.ini"
cat > "$fixture/game/start-server.sh" <<'GAME'
#!/usr/bin/env python3
import sys
with open('/zomboid/commands', 'w', buffering=1) as f:
    for line in sys.stdin:
        f.write(line)
        if line.strip() == 'quit': break
GAME
chmod -R a+rwX "$fixture/game" "$fixture/data"
chmod +x "$fixture/game/start-server.sh"
container=$(docker run -d --init -e PZ_ADMIN_PASSWORD=fixture-only -e RCON_PASSWORD=fixture-only -v "$fixture/game:/pz-server" -v "$fixture/data:/zomboid" local/pz-server:runtime-1)
trap 'docker rm -f "$container" >/dev/null 2>&1 || true' EXIT
for _ in {1..20}; do [[ -f "$fixture/data/commands" ]] && break; sleep 0.25; done
[[ -f "$fixture/data/commands" ]]
# The maintenance lock must refuse updates even in a separate container.
if docker run --rm -v "$fixture/game:/pz-server" local/pz-server:runtime-1 update-only; then
    echo 'ERROR: maintenance lock was bypassed' >&2; exit 1
fi
docker stop --time 30 "$container" >/dev/null
[[ "$(docker inspect -f '{{.State.ExitCode}}' "$container")" == 0 ]]
docker logs "$container"
python3 - "$fixture" <<'PY'
import json, pathlib, sys
p=pathlib.Path(sys.argv[1])
assert (p/'data/commands').read_text() == 'save\nquit\n'
assert json.loads((p/'game/ProjectZomboid64.json').read_text())['vmArgs'] == ['-Xms4g','-Xmx8g','-Dkeep.fixture=true']
print('PASS: SIGTERM -> save/quit -> exit 0; JVM flags preserved; update lock enforced')
PY
