"""Standalone PZ lifecycle. No panel dependency; SIGTERM saves and quits PZ."""
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import socket
import struct
import subprocess
import sys
import time


def log(message):
    print(f"[pz-runtime] {message}", flush=True)


def receive(sock):
    def exact(length):
        data = b""
        while len(data) < length:
            chunk = sock.recv(length - len(data))
            if not chunk:
                raise ConnectionError("RCON connection closed")
            data += chunk
        return data
    length = struct.unpack("<i", exact(4))[0]
    if not 10 <= length <= 4 * 1024 * 1024:
        raise ValueError("Invalid RCON packet length")
    data = exact(length)
    request_id, kind = struct.unpack("<ii", data[:8])
    return request_id, kind, data[8:-2].decode("utf-8", errors="replace")


def rcon(command, host=None):
    host = host or os.environ.get("RCON_HOST", "127.0.0.1")
    with socket.create_connection((host, int(os.environ.get("RCON_PORT", "27015"))), timeout=10) as sock:
        sock.settimeout(15)
        def send(request_id, kind, value):
            payload = struct.pack("<ii", request_id, kind) + value.encode() + b"\0\0"
            sock.sendall(struct.pack("<i", len(payload)) + payload)
        send(1, 3, os.environ["RCON_PASSWORD"])
        for _ in range(4):
            request_id, kind, _ = receive(sock)
            if request_id == -1:
                raise PermissionError("RCON authentication failed")
            if request_id == 1 and kind == 2:
                break
        else:
            raise ConnectionError("No RCON authentication response")
        send(2, 2, command)
        for _ in range(8):
            request_id, _, text = receive(sock)
            if request_id == 2:
                return text
        raise ConnectionError("No RCON command response")


def run():
    mode = sys.argv[1] if len(sys.argv) > 1 else "run"
    if mode == "health":
        rcon("players")
        return 0
    if mode == "rcon":
        print(rcon(" ".join(sys.argv[2:])))
        return 0
    if mode not in ("run", "update-only"):
        raise ValueError("Expected run, update-only, health, or rcon COMMAND")
    name = os.environ.get("PZ_SERVER_NAME", "survival42")
    branch = os.environ.get("PZ_BRANCH", "public")
    for value in (name, branch):
        if not re.fullmatch(r"[A-Za-z0-9._-]+", value):
            raise ValueError("Invalid server name or branch")
    # A maintenance container cannot update files beneath a running instance.
    with open("/pz-server/.runtime.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if mode == "update-only":
            args = ["/home/steam/steamcmd/steamcmd.sh", "+force_install_dir", "/pz-server", "+login", "anonymous", "+app_update", "380870"]
            if branch != "public":
                args += ["-beta", branch]
            if os.environ.get("PZ_VALIDATE_ON_UPDATE", "false").lower() == "true":
                args += ["validate"]
            # JVM flags apply only to the game, never SteamCMD.
            env = {k: v for k, v in os.environ.items() if k != "JAVA_TOOL_OPTIONS"}
            return subprocess.call(args + ["+quit"], env=env)
        if not Path("/pz-server/start-server.sh").is_file():
            raise RuntimeError("PZ is not installed. Run the explicit update-only maintenance command first.")
        if not Path(f"/zomboid/Server/{name}.ini").is_file():
            raise RuntimeError("Server configuration is missing; refusing to create a new world")
        config_path = Path("/pz-server/ProjectZomboid64.json")
        config = json.loads(config_path.read_text())
        memory = [os.environ.get("PZ_XMS", "4g"), os.environ.get("PZ_XMX", "8g")]
        if not all(re.fullmatch(r"[1-9][0-9]*[mMgG]", x) for x in memory):
            raise ValueError("Invalid JVM memory setting")
        config["vmArgs"] = [f"-Xms{memory[0]}", f"-Xmx{memory[1]}"] + [x for x in config["vmArgs"] if not re.match(r"^-Xm[sx]", x, re.I)]
        config_path.write_text(json.dumps(config, indent=2) + "\n")
        password = os.environ.get("PZ_ADMIN_PASSWORD")
        if not password:
            raise RuntimeError("PZ_ADMIN_PASSWORD is required")
        stopping = False
        def stop(_signum, _frame):
            nonlocal stopping
            stopping = True
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        log("Starting game; no Steam update on startup")
        with open("/zomboid/server-console-docker.log", "ab", buffering=0) as output:
            child = subprocess.Popen(["/pz-server/start-server.sh", "-servername", name, "-adminpassword", password], stdin=subprocess.PIPE, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
            shutdown_sent = False
            while child.poll() is None:
                if stopping and not shutdown_sent:
                    shutdown_sent = True
                    log("Shutdown requested: saving world and requesting graceful quit")
                    try:
                        rcon("save")
                        rcon("quit")
                    except (OSError, ValueError) as error:
                        log(f"RCON shutdown unavailable ({type(error).__name__}); sending save/quit to game console")
                        try:
                            child.stdin.write(b"save\nquit\n")
                            child.stdin.flush()
                        except (BrokenPipeError, OSError):
                            log("Console closed; waiting for game process to exit")
                time.sleep(0.5)
            log(f"Game process exited with code {child.returncode}")
            return 0 if stopping and child.returncode == 0 else child.returncode


if __name__ == "__main__":
    try:
        sys.exit(run())
    except Exception as error:
        # Never include connection credentials or the game command line.
        log(f"Failed: {type(error).__name__}: {error}")
        sys.exit(1)
