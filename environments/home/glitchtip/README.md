# GlitchTip on updspace-home

The operator chose GlitchTip instead of the full Sentry deployment. Errors and
transactions/spans use Sentry SDKs; existing Prometheus and Loki handle infrastructure
metrics and logs. Sentry profiling and session replay are not provided.

One k3s pod, pinned GlitchTip 6.2.6 image digest, request 125m CPU/512Mi RAM,
limit 1 CPU/1Gi RAM. Measured idle usage with span storage was 179Mi / 27m CPU;
this is not a load test. Embedded DuckDB is limited to 128MB, under the same pod limit.
There is no separate Redis, Kafka or ClickHouse. PostgreSQL is the existing shared
instance, with a separate `glitchtip` database and role (12 connections maximum).
Its existing memory/disk budget is shared, not counted in the application pod limit.

Errors retain 14 days, transactions and raw spans 7 days, uploaded files 30 days.
Spans use local Parquet storage under `/code/uploads/cold-storage`; disabling
DuckDB also disables detailed span storage in this release. Uploads live at
`/srv/glitchtip/uploads`, UID/GID 5000, retained PV. The declared 5Gi PV capacity
is not a filesystem quota. Backups have no automatic deletion policy.

## Access and bootstrap

https://errors.updspace.com uses the common Caddy, with Cloudflare proxy enabled.
UI requires an active UpdSpace ID account with BOTH staff and system_admin,
followed by the GlitchTip login
`admin@updspace.com`. Passwords and the initial project DSN are in the VM's root-only
`/opt/updspace-infra/private/glitchtip-credentials.json`; never commit that file.
`bootstrap.py` preserves existing credentials and rejects unexpected database ownership
or role privileges. Run without arguments for a read-only state check, then explicitly
`--apply database` before deployment and `--apply account` after migrations are ready.
Kubernetes runtime Secret is `glitchtip/glitchtip-runtime`.

Public SDK POST/OPTIONS requests bypass the ID browser gate only on numeric project
`/api/<id>/envelope/` and `/api/<id>/minidump/` paths; GlitchTip still verifies the DSN.
Signup, email delivery, uptime checks and log ingestion are disabled. ID role
checks are live on every UI/API request; project ingestion remains DSN-authenticated.
The existing `UpdSpace / Infra Smoke` project contains labelled synthetic acceptance
events. Application projects/SDK settings remain an explicit application rollout.

After the ID gate rollout, public Cloudflare ingestion accepted and persisted an
error, transaction and child span (`infra-acceptance-b01764ab0849`). On the operator
workstation the explicitly approved hosts block routes only five monitoring domains
to the home origin with normal TLS verification. Chromium loaded GlitchTip and
signed in without JavaScript errors before the ID gate cutover. Full interactive
ID login has not yet been verified with the operator account. Other computers do
not receive this hosts change; Cloudflare stays proxied externally.

Kuma checks the internal `/_health/` endpoint every five minutes. Its scoped
NetworkPolicies are part of `resources.yaml`, and the monitor is declared in
`../uptime-kuma/config.json`. This is not a public HTTPS acceptance check.

Apply `resources.yaml` separately after the database bootstrap and runtime Secret
exist; `scripts/render.py --scope all` does not include this component. The edge
route also requires its scoped egress policy from this file.

## Backup and restore

Install `backup.sh` byte-for-byte at `/opt/glitchtip/backup.sh`, root-owned, and the
two `glitchtip-backup.*` units under `/etc/systemd/system`. The daily timer runs
around 03:30 Europe/Moscow. It briefly stops only GlitchTip for a coherent database
and uploads snapshot, then restores its single replica, including on ordinary errors.
A hard process kill/power loss during backup can require manually restoring replicas.
PostgreSQL and other services remain running. Do not concurrently scale GlitchTip
while this backup holds `/run/lock/glitchtip-backup.lock`.

Snapshots: `/srv/updspace/backups/glitchtip/<UTC>-<suffix>`. Encryption uses the
existing public `/opt/updspace-data/backup-recipient.asc`; its private key remains
on the operator workstation. The encrypted archive includes the dump, uploads and
GlitchTip credentials. `COMMITTED` is written only after the encrypted hash is verified.

On the workstation install `sync-backups.py` into
`~/.local/share/updspace-backups/infra-sync/glitchtip/`, and the unchanged
`../portal/sync-postgres-backups.py` into the sibling `infra-sync/portal/` directory.
Install/enable `glitchtip-offsite.*` as user units. Only ciphertext is transferred;
its SHA256 is checked before the local `COMMITTED` marker is written.

`python3 verify-backup.py <local-backup-directory>` decrypts in workstation memory
and restores to a disposable PostgreSQL pod with emptyDir, no Service, and loopback
binding. It checks restored users, projects, errors, transactions and spans. It does
not start a replacement production application or overwrite the working database.

## Checks

```sh
python3 -m unittest discover -s environments/home/glitchtip -p 'test_*.py'
sudo python3 environments/home/glitchtip/verify-ingest.py
```

The second command creates labelled synthetic events and verifies persisted error,
transaction and child span records. `--origin-ip 192.168.1.176` checks the origin
separately. A 2026-10-09 offsite restore recovered 1 user, 1 project, 3 errors,
3 transactions and 1 span; uploads were empty, so real uploaded-file restoration
has not yet been exercised. No production application telemetry is wired automatically.
