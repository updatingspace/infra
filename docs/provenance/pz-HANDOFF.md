# PZ → central infra handoff (2026-10-09, read-only inventory)

Source Git root: `/home/m4tveevm/PycharmProjects/zomboid/infra-repo`.
Current branch: `feat/pz-game-auto-update`; HEAD `ff6f65a`.
Existing remote: `github.com/updatingspace/infra`, distinct from the future `github.com/updspace/infra`.
Migration/local-monitoring/local-edge changes are uncommitted; the latter two directories are untracked. Copy the WORKING TREE, not `git archive HEAD`. No commit/push or live changes performed during this inventory.

## Exact source set

`source-files.txt` lists 157 source files relative to the source Git root. Preserve their full `environments/pz/kubernetes/...` paths in the central repository. `source-sha256.json` records the handoff contents; a mismatch means another task changed a source after this inventory, not permission to overwrite its changes. Include provider `.terraform.lock.hcl` files and example tfvars; merge the original root `.gitignore` exclusions into the central ignore file.

This complete Kubernetes subtree is the minimum self-contained source bundle retaining the existing PZ IaC, helpers, tests, policy and recovery tooling. Do not flatten it: workloads references `../panel-telemetry`, `../panel-updater`; local-monitoring reads `../observability/otel-collector.yaml`; game-updater imports `../backup/metrics.py` and parent deployment helpers. `deploy.py` normally deploys only platform/workloads/observability into `/opt/pz-infrastructure`; local-edge/local-monitoring and host setup require explicit orchestration.

## Live component → source mapping

| Live component | Source under environments/pz/kubernetes | State/data/installation |
|---|---|---|
| k3s v1.36.4+k3s1, node updspace-home | bootstrap/ (historical cloud bootstrap; local adoption still needed) | `/etc/rancher/k3s`, `/var/lib/rancher/k3s`; never commit kubeconfig/token/db |
| Namespace quotas, LimitRange, baseline deny/DNS rules | platform/ | Live quota values are in live-inventory.json; deployment tfvars are private |
| Zomboid StatefulSet, panel, Services, retained PV/PVC | workloads/ | `/srv/pz-storage/zomboid`, symlinks `/opt/pz-stack/data/*`; local game ext4 64 GiB |
| Panel telemetry and suspended updater CronJob | panel-telemetry/, panel-updater/, workloads/panel-*.tf | ConfigMaps match source hashes; CronJob suspended |
| Shared Caddy | workloads/caddy.tf + local-edge/ | Caddy's live config differs; central task owns reconciliation. Actual PV paths `/srv/edge-caddy/{caddy-data,caddy-config}` on 256 MiB ext4 |
| Collector | observability/ + local-monitoring/collector.yaml | Live collector ConfigMap matches local YAML; host mounts `/`, game/panel logs, `/opt/pz-stack/data/otelcol` |
| Prometheus, Grafana, Loki, Alertmanager | local-monitoring/build.py → resources.json | All five generated ConfigMaps match live exactly; data `/srv/pz-monitoring/{prometheus,grafana,loki,alertmanager}` via hostPath |
| Backup capture, S3 uploader, cleanup, retention, recovery, metrics | backup/*.py, backup/systemd/*, backup/install.py | Host services `/opt/pz-backup`, venv `/opt/pz-backup-venv`, `/etc/pz-backup`, journals `/var/lib/pz-backup*` |
| Game update service/timer | game-updater/ | `/opt/pz-game-update`; timer currently disabled; coordinates with backup lock |
| S3 bucket, actor accounts and permissions | backup-cloud/ | S3 retained; two cloud disks deleted and Terraform removed/forget applied. Private state remains outside Git |
| Historical cloud helper | cloud/, storage/, storage-growth/ | Helper stays RUNNING for Minecraft. Never reapply historical storage bootstrap to local volumes blindly |

`live-inventory.json` contains the exact images, requests/limits, Services, quotas, LimitRanges, PV/PVC specs and current NetworkPolicies, without env values or Secrets. `config-parity.json` and `host-parity.json` record source comparisons. `host-storage.json` records actual filesystem layout and compatibility symlinks.

## Effective limits and gaps

- Game: 2200m CPU / 10 GiB RAM; request 1500m / 8 GiB. Panel: 300m / 768 MiB. Suspended panel updater: 100m / 128 MiB. Zomboid namespace total quota: 2600m / 11136 MiB; requests quota 2 CPU / 8512 MiB.
- Caddy: 200m / 256 MiB, equal to its namespace limits quota.
- Monitoring: Prometheus 300m/1 GiB, Grafana 200m/768 MiB, Loki 300m/1 GiB, Alertmanager 100m/128 MiB, collector 200m/512 MiB. Namespace limits quota 1100m/3584 MiB.
- Host backup/capture/recovery and updater units: CPUQuota 100%, MemoryMax 2 GiB; upload/cleanup/retention: 50%, 512 MiB; exporter: 5%, 64 MiB. These are OUTSIDE Kubernetes namespace quotas. Enabled daily backup at 06:00 Europe/Moscow, retry uploader timer enabled; metrics service active; game updater disabled, retention enable flag absent.
- `host-overrides.json` contains five unmapped nonsecret overrides: backup schedule, exporter bind 192.168.1.176, k3s game/spool mount dependency, shared edge mount dependency, and srv-edge\\x2dcaddy.mount. These need declarative host provisioning or an explicitly designed k3s replacement. All 23 directly mapped installed PZ Python/unit files match current source.
- `backup-settings-public.json` contains only checked nonsecret live installer settings, filesystem UUIDs, public age recipient, S3 scope and enable flags. Uploader numeric GID is host-specific. Credentials were not read/exported.
- Two 64 GiB ext4 loop files and the edge 256 MiB loop share the same physical SSD. Their creation/UUID adoption, ownership, fstab and systemd dependencies are not fully represented by reusable local IaC yet. `storage/main.tf` describes HISTORICAL 26 GiB game layout, not these volumes.
- Actual `/srv/pz-storage/observability` is currently a directory on root filesystem, not a dedicated mounted 512 MiB volume. Collector state and local-monitoring hostPaths are not bounded by PVC storage quotas. Prometheus 15d/15GB and Loki 7d retention are application retention, not disk-space isolation.
- Game `Server/*.ini`/`*.lua` configuration resides in persistent game data and is not fully declarative IaC. INI can contain RCON/server passwords; do not commit raw files. Split nonsecret settings from injected secrets while preserving the world. Keep `Public=false` per user preference; do not publish in Steam for monitoring.
- Shared live policies include caddy-to-garage, caddy-to-id, caddy-to-monitoring, monitoring-from-caddy, health-from-uptime-kuma. Other tasks own these additions; do not revert them with the older PZ-only manifest. The central task is already changing Caddy/monitoring access, so this inventory is a point-in-time snapshot.
- Old local-monitoring README still says cloud world is retained for rollback. Correct when consolidating: the two additional cloud disks were deleted after full-copy checks; retained local full copies and S3 remain. `/opt/pz-stack/data/caddy-*` symlinks still refer to the older edge directory, while live Caddy PVs use `/srv/edge-caddy`; do not infer active Caddy storage from those legacy links.
- Host→k3s migration of backup/update jobs is not implemented by this handoff. Preserve maintenance locking, clean stop/save evidence, mount checks, scoped S3 credentials, readback/COMMITTED and tested restore semantics when designing it.

## Exclude from Git and source copying

Exclude all `private/`, `operator-backups/`, `.terraform/`, `.apply/`, `*.tfstate*`, `*.tfplan`, actual `*.tfvars*` (keep only explicitly named example files), logs/receipts/archives, `.env`/runtime env, key/pem/kubeconfig files, credentials and generated runtime data. Do not copy an entire operator directory into the central repo.

In particular: `/etc/pz-backup/upload.credentials.json`; any future retention credentials; workstation age private key; `/etc/rancher/k3s/k3s.yaml` and cluster tokens; Grafana password files and grafana-admin Secret; panel/game Kubernetes Secrets; raw server INI; raw recovery captures; private Terraform backend/state and provider authentication. The handoff's source-files manifest excludes these, but is not a replacement for reviewing newly added files before committing.

Retain separately on the local VM: `/srv/pz-migration/20261009` (verified copies/restore evidence), all live game/monitoring data and `/srv/pz-backup-spool`. No data moved/deleted by this inventory. S3 is unchanged. The handoff does not prove external gameplay availability or audit every other VM namespace.
