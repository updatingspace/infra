# ID / YDB / Garage handoff to updspace/infra

Frozen source root: `/home/m4tveevm/PycharmProjects/id/infra/k3s`.
Copy the files listed in `HANDOFF.sha256`, preserving `id/`, `garage/` and
the root README together. Suggested central destination:
`environments/home/id-platform/`. Relative paths in the renderers and runbook
then remain valid. Do not copy `__pycache__`, runtime directories or secrets.

## Live versus staged

- Garage is live shared infrastructure, with ID and Portal isolated keys/buckets.
  Its manifest, desired layout/key permissions and S3 CORS/lifecycle settings
  are included. Bootstrap CLI operations were performed manually and verified;
  no idempotent layout/key reconciler is claimed. Do not recreate keys on adoption.
- YDB is live **trial** infrastructure. A consistent source snapshot was restored
  and compared table-by-table. `ydb-config.yaml` is the real nonsecret config.
  The initialized PDisk contains auth/cluster state: manifest alone is not bootstrap.
- ID application is 6/6 Ready on trial data; public DNS is still Yandex Cloud.
  `applications.yaml` deliberately defaults to replicas 0, all CronJobs suspended.
  Do not blindly apply it over the running trial or treat it as production desired state.
- One complete encrypted ID backup is already on the workstation and passed an
  isolated restore. VM daily backup units are installed but **disabled**. Workstation
  offsite timer is enabled. Restore and synthetic browser/auth evidence is included.
- Production cutover requires a user decision about unsupported 80GiB file-backed
  YDB, a final writer freeze/snapshot and data replacement. No production switch occurred.

## Shared edge changes already applied

The only files this task edited in the central repository are:

- `environments/home/edge/Caddyfile`: appended LAN-only `id.updspace.com` trial route.
- `environments/home/edge/resources.json`: added Secret volume `id-origin-tls` and mount.

Certificate/private key are **not in Git**. `edge/id-origin-tls` contains a dedicated
Let's Encrypt certificate valid until 2027-01-07. It was issued with manual DNS-01;
that is a pre-cutover certificate, not an automatic renewal solution. On cutover,
transfer TLS management to Caddy ACME before declaring renewals ready.
The temporary TXT challenge was removed. Existing cloud wildcard key was not exported.

`id/caddy-to-id.yaml` is the already-live edge egress policy missing from the original
central snapshot. Its identical object also appears in `id/applications.yaml`;
import it **once** under shared edge ownership when combining manifests.
It permits only ID pod labels `app.kubernetes.io/name=id` and
`app.kubernetes.io/part-of=updspace-id` in namespace `updspace-id`, TCP 8089.
Preserve all existing Caddy routes, monitoring authentication and egress policies.
The canonical Caddyfile and legacy compatibility copy on VM are synchronized.
The canonical remote `resources.json` mirror should be refreshed from the final commit.

DNS: only `storage.updspace.com` is DNS-only (same DDNS CNAME, TTL60), with documented
large-transfer failure through proxy and successful 10MiB browser acceptance direct.
No ID DNS change. Observability proxy choice belongs to the infra task and is unchanged.

## State and secret ownership

| Location | Owner / contents |
| --- | --- |
| `/opt/updspace-id` | root-only runtime input, local images, backup code, ACME state |
| `/opt/updspace-data/id-ydb` | root-only YDB passwords/TLS/bootstrap evidence |
| `/srv/updspace/id-ydb` | UID65534 YDB state, Retain PV, sparse80GiB PDisk |
| `/opt/updspace-data/garage` | root-only shared Garage keys/admin/RPC credentials |
| `/srv/updspace/garage` | UID1000 Garage data/metadata, Retain PV |
| `/srv/backups/updspace-id` | root-only backup staging, source snapshot and resume marker |
| `/srv/backups/updspace-id-encrypted` | completed encrypted ID bundles, readable by VM SSH user |
| workstation `~/.local/share/updspace-backups/id` | encrypted source/local backups and sync script |
| workstation `~/.local/share/updspace-backups/keys` | private decrypt key; never copied to VM |

ID runtime secrets are `updspace-id/id-{api,sessions,mutations,web,jobs}`;
DB secrets are `updspace-data/id-ydb-{admin,runtime,tls}`; the public CA is
`updspace-id/id-ydb-ca`. Garage has distinct ID and Portal runtime credentials.
ID offsite bundle does not contain the Portal Garage key or business data.

Limits/requests are explicit in each manifest. Local-volume PV capacity and
Garage advertised layout capacity are not filesystem quotas; retain disk monitoring.
Source application commit, pinned image digests, build/test evidence and remaining
limitations are documented in README. Images are imported locally, not registry-published.

## Validation

`python3 id/render_routes.py --check`, `python3 id/test_sync_backups.py`, YAML parse
and server-side dry-run passed. Caddy validated and reloaded; ID origin HTTPS/login
passed without disabling TLS. Existing status/storage/observability responses remained
correct. Production-source Rust tests: 96 passed/20 intentionally ignored, four HTTP
export tests passed. Runtime TLS auth, password/TOTP/session and real Chromium passed
on trial data. Full CI, real OAuth providers/passkeys and mail delivery remain unchecked.
