# Local monitoring

Single-node stack for `updspace-home` (`192.168.1.176`), in the existing
`observability` namespace. Platform Terraform owns its quota and default-deny
policies. This directory adds Prometheus, Grafana, Loki and Alertmanager; the
existing collector sends metrics and redacted logs to these local backends.

Run `python3 build.py` after changing the source, then
`python3 -m unittest discover -s . -p 'test_*.py'`.
Review `resources.json` and use `kubectl apply --dry-run=server` before applying.
Collector Terraform needs `monium_secret_name = null` and
`collector_config_path = "../local-monitoring/collector.yaml"`.

Host data directories under `/srv/pz-monitoring` must exist before deployment:
`prometheus` and `alertmanager` belong to UID/GID 65534, `grafana` to 472,
and `loki` to 10001. Create the `grafana-admin` Secret with a private random
`password` separately; never put it in these manifests.

Grafana is at `https://grafana.updspace.com`; Prometheus at
`https://prometheus.updspace.com`; Alertmanager at `https://alerts.updspace.com`.
Shared Caddy requires an active UpdSpace ID account with BOTH staff and system_admin.
Grafana automatically completes native OAuth with ID, creates personal accounts by
stable `sub`, and synchronizes login (`preferred_username`), name/email and organization Admin access on login.
Server administrator is never granted. Password/basic login is disabled; existing
credentials and accounts remain for recovery. Provision `grafana-oidc` from
`../observability-auth/provision-client.py --apply` and apply its two Grafana↔ID
NetworkPolicies before this deployment. Services are ClusterIP; the former LAN
NodePort 30030 is closed. Loki remains internal. Caddy reaches the services through
`../edge/monitoring-network.yaml`; session and role authorization is documented in
`docs/id-access.md` at repository root.

Prometheus retains 15 days, capped at 15 GB; Loki retains seven days.
These retention settings are not filesystem quotas. The four backends use
up to 900m CPU and 2944 MiB RAM; the existing collector adds 200m and 512 MiB.

The Alertmanager receiver currently exposes alerts in the local UI only.
External notification delivery must be configured and tested separately.
The 22 rules preserve game and backup checks, including 26/30-hour backup
age thresholds, missing mounts, free space/inodes, failed or stalled jobs,
multipart uploads and stale restore evidence. Backup thresholds follow
`../../pz/kubernetes/observability/backup-alerts.json`. Validate the generated `rules.yml` with
`promtool check rules`; `rules.test.yml` exercises warning/critical separation
and the 15-minute mount-loss delay (place the generated rules at
`/tmp/pz-alert-rules.yml`, then run `promtool test rules rules.test.yml`).
Game availability and backup freshness alerts do not prove client gameplay
or a successful runtime restore drill.

The migration's private evidence and logs are under
`/srv/pz-migration/20261009`. The original cloud world is retained for rollback.
Rollback requires stopping and saving the local game first, preserving any new
local progress, and coordinating cloud startup and DNS; never run both worlds
as independently writable production servers.

Native OAuth regression acceptance uses the real pinned Grafana image and an
isolated mock IdP, with a disposable tmpfs database and no production credentials:

```sh
GRAFANA_IMAGE=docker.io/grafana/grafana:13.2.3@sha256:b28bae15e219c998fb0e0424ed724930cc61b1f61fb404d47c862f9a23f9e572 python3 -m unittest discover -s environments/home/monitoring -p 'test_*.py'
```

CI runs this test; Docker is required. It verifies PKCE, denied role combinations,
automatic account creation and profile updates retaining the same account ID.

The organization is declared in `organization.json`. On the VM,
`sudo python3 configure-organization.py --apply` renames only the existing org 1
from Main Org. to UpdatingSpace LLC. It briefly stops Grafana, writes a verified
private SQLite backup and restarts the same deployment. Check mode does not write.
Users, memberships and dashboard IDs are preserved; other names are rejected.
