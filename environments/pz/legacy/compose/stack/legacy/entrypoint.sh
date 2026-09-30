#!/usr/bin/env bash

set -Eeuo pipefail

STEAM_UID=1000
STEAM_GID=1000

STEAMCMD="/home/steam/steamcmd/steamcmd.sh"
PZ_APPID="380870"

PZ_ADMIN_PASSWORD="${PZ_ADMIN_PASSWORD:-}"
PZ_BRANCH="${PZ_BRANCH:-public}"
PZ_SERVER_NAME="${PZ_SERVER_NAME:-survival42}"
PZ_UPDATE_ON_START="${PZ_UPDATE_ON_START:-false}"
PZ_VALIDATE_ON_UPDATE="${PZ_VALIDATE_ON_UPDATE:-false}"
PZ_AUTOSTART="${PZ_AUTOSTART:-true}"
PZ_XMS="${PZ_XMS:-4g}"
PZ_XMX="${PZ_XMX:-8g}"

MODE="${1:-run}"

log() {
    printf '[entrypoint] %s\n' "$*"
}

is_true() {
    case "${1,,}" in
        1|true|yes|on) return 0 ;;
        *) return 1 ;;
    esac
}

validate_safe_value() {
    local name="$1"
    local value="$2"

    if [[ ! "$value" =~ ^[A-Za-z0-9._-]+$ ]]; then
        printf '[entrypoint] ERROR: unsafe value for %s: %q\n' \
            "$name" "$value" >&2
        exit 1
    fi
}

run_as_steam() {
    local escaped=""
    local arg

    for arg in "$@"; do
        printf -v escaped '%s%q ' "$escaped" "$arg"
    done

    su steam -s /bin/bash -c "$escaped"
}

prepare_directories() {
    mkdir -p \
        /pz-server \
        /zomboid \
        /zomboid/Server \
        /zomboid/mods \
        /app/data \
        /app/logs \
        /home/steam/Steam

    chown -R "${STEAM_UID}:${STEAM_GID}" \
        /pz-server \
        /zomboid \
        /app/data \
        /app/logs \
        /home/steam/Steam

    # Stock PZ scripts normally use ~/Zomboid.
    # This symlink makes the persistent /zomboid bind mount authoritative.
    if [[ -d /home/steam/Zomboid && ! -L /home/steam/Zomboid ]]; then
        log "Migrating existing /home/steam/Zomboid data into /zomboid."
        cp -a /home/steam/Zomboid/. /zomboid/ 2>/dev/null || true
        rm -rf /home/steam/Zomboid
    fi

    if [[ ! -e /home/steam/Zomboid ]]; then
        ln -s /zomboid /home/steam/Zomboid
    fi

    chown -h "${STEAM_UID}:${STEAM_GID}" /home/steam/Zomboid
}

update_server() {
    local validate="$1"
    local -a command

    command=(
        "$STEAMCMD"
        +force_install_dir /pz-server
        +login anonymous
        +app_update "$PZ_APPID"
    )

    if [[ "$PZ_BRANCH" != "public" && -n "$PZ_BRANCH" ]]; then
        command+=(-beta "$PZ_BRANCH")
    fi

    if is_true "$validate"; then
        command+=(validate)
    fi

    command+=(+quit)

    log "Running SteamCMD update for app ${PZ_APPID}, branch ${PZ_BRANCH}."
    run_as_steam "${command[@]}"

    chown -R "${STEAM_UID}:${STEAM_GID}" /pz-server
    chmod 0755 /pz-server/start-server.sh 2>/dev/null || true
}

patch_jvm_memory() {
    local json="/pz-server/ProjectZomboid64.json"

    if [[ ! -f "$json" ]]; then
        log "ProjectZomboid64.json is not present; JVM patch skipped."
        return 0
    fi

    PZ_JSON="$json" \
    PZ_XMS_VALUE="$PZ_XMS" \
    PZ_XMX_VALUE="$PZ_XMX" \
    node <<'NODE'
const fs = require("fs");

const file = process.env.PZ_JSON;
const xms = process.env.PZ_XMS_VALUE;
const xmx = process.env.PZ_XMX_VALUE;

const config = JSON.parse(fs.readFileSync(file, "utf8"));

if (!Array.isArray(config.vmArgs)) {
  throw new Error(`${file}: vmArgs is not an array`);
}

const preserved = config.vmArgs.filter(
  (arg) => !/^-Xm[sx]/i.test(String(arg))
);

config.vmArgs = [
  `-Xms${xms}`,
  `-Xmx${xmx}`,
  ...preserved
];

fs.writeFileSync(file, `${JSON.stringify(config, null, 2)}\n`);
NODE

    chown "${STEAM_UID}:${STEAM_GID}" "$json"
    log "JVM memory configured: Xms=${PZ_XMS}, Xmx=${PZ_XMX}."
}

autostart_server() {
    local ini="/zomboid/Server/${PZ_SERVER_NAME}.ini"

    if ! is_true "$PZ_AUTOSTART"; then
        log "PZ_AUTOSTART is disabled."
        return 0
    fi

    # First boot must be initialized by the panel setup wizard.
    if [[ ! -f "$ini" ]]; then
        log "No ${ini}; skipping game autostart until setup wizard completes."
        return 0
    fi

    if pgrep -f 'zombie\.network\.GameServer' >/dev/null 2>&1; then
        log "Project Zomboid server already appears to be running."
        return 0
    fi

    log "Starting Project Zomboid server '${PZ_SERVER_NAME}'."

    if [[ -z "${PZ_ADMIN_PASSWORD:-}" ]]; then
    log "ERROR: PZ_ADMIN_PASSWORD is empty; refusing non-interactive first start."
    return 1
    fi

    su steam -s /bin/bash -c \
        "cd /pz-server && \
         nohup ./start-server.sh \
           -servername '${PZ_SERVER_NAME}' \
           -adminpassword '${PZ_ADMIN_PASSWORD}' \
           >> /zomboid/server-console-docker.log 2>&1 &"
}

main() {
    validate_safe_value "PZ_BRANCH" "$PZ_BRANCH"
    validate_safe_value "PZ_SERVER_NAME" "$PZ_SERVER_NAME"

    if [[ ! -x "$STEAMCMD" ]]; then
        log "ERROR: SteamCMD not found at ${STEAMCMD}."
        exit 1
    fi

    prepare_directories

    if [[ ! -x /pz-server/start-server.sh ]]; then
        log "No PZ installation found; performing initial install with validation."
        update_server true
    elif [[ "$MODE" == "update-only" ]]; then
        update_server "$PZ_VALIDATE_ON_UPDATE"
    elif is_true "$PZ_UPDATE_ON_START"; then
        update_server "$PZ_VALIDATE_ON_UPDATE"
    else
        log "Existing PZ install retained; automatic update disabled."
    fi

    patch_jvm_memory

    if [[ "$MODE" == "update-only" ]]; then
        log "Update-only operation completed."
        exit 0
    fi

    autostart_server

    cd /app
    exec su steam -s /bin/bash -c \
        "cd /app && exec node server/index.js"
}

main "$@"
