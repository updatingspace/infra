# Declarative survival42 settings

Captured from the existing local server on 2026-10-09. This package declares 137
reviewed nonsecret INI fields and the three active survival42 Lua configuration
files. It does not contain a raw server INI. `Public=false` stays mandatory;
Steam catalog publication is not enabled. Mod IDs and game settings are captured
as deployed, without upgrading the game or modifying the world.

`survival42.ini.json` contains the desired values. `policy.py` is an explicit
allowlist, not a secret-name blacklist. Existing Password, RCONPassword,
DiscordToken and WebhookAddress are untouched. ResetID, ServerPlayerID and Seed
remain unmanaged world identity. Every unknown/new INI key remains untouched;
adding it to desired JSON is rejected until the allowlist is deliberately reviewed.
Nonsecret names/welcome text/channel IDs are included as infrastructure metadata.

INI updates preserve all unowned bytes, comments, ordering and line endings.
Duplicates, missing managed keys, newline injection and symlink paths fail closed.
Lua files are complete reviewed source files. Before overwriting changed Lua,
the existing file must match `lua-base-sha256.json`; unexpected live edits cause
refusal. When intentionally updating Lua, set the baseline to the previously
reviewed deployed version and update desired source together. Changed Lua needs
an existing `luac` executable for `luac -p` validation; the tool does not install
packages or execute Lua. An unchanged captured Lua file needs no compiler.

## Check and controlled application

Run on the target VM with the whole package in one trusted directory:

```sh
sudo python3 configure.py --check
```

Exit 0 = matches, 2 = reviewed drift, 1 = validation/refusal. Output contains
changed filenames and key names, never current INI values. Tests run locally:

```sh
python3 -m unittest discover -s . -p 'test_*.py' -v
```

Only after an operator has cleanly saved/stopped the game, scaled both the game
StatefulSet and panel Deployment to zero and suspended the panel updater:

```sh
sudo python3 configure.py --apply
```

The tool does not stop/start/scale anything. Apply requires root, the existing
trusted `/opt/pz-backup/coordinator.py`, its shared maintenance lock and completed
backup/disk-migration/panel-updater journals. It validates the existing game's
mount UUID from `/etc/pz-backup/config.json`, checks replicas and absence of active
or terminating PZ pods, then re-reads files under the lock. Other operators must
respect the same maintenance lock; do not restart workloads during application.
This relies on the existing PZ backup installation and is not a clean-host installer.

Only the four fixed files under `/srv/pz-storage/zomboid/zomboid/Server` can change.
Originals go into a private 0700 directory under `/var/lib/pz-game-config`, retaining
metadata. These originals INCLUDE secrets and must never enter Git or source
artifacts. Each file is atomically replaced with readback verification; an ordinary
failure restores originals. A durable `pending.json` blocks replay after a process
or host crash. For that exceptional case, keep workloads stopped, inspect the named
private backup, restore the affected config files with their ownership/permissions,
and remove the pending marker only after verification. There is no blind retry.
The tool leaves backup copies for controlled recovery and never deletes save data.

The nonsecret profile was checked read-only against the current VM. Apply/rollback
were tested only with isolated temporary fixtures; no production application or
restart was performed. This package is not yet wired into central deployment or a
k3s Job; its host maintenance semantics must be preserved when integrating it.
