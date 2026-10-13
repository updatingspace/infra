# ID on the home VM

This overlay runs one ID deployment and the five existing CronJobs. The base
alone has zero replicas and suspended schedules. On a replacement node, restore
YDB, ID secrets and Garage objects and import the exact pinned images first.

```sh
sudo k3s kubectl kustomize . > /tmp/id-production.yaml
sudo k3s kubectl apply --dry-run=server -f /tmp/id-production.yaml
sudo k3s kubectl diff -f /tmp/id-production.yaml
```

Review drift before applying. The overlay does not install secrets, import data,
change DNS, modify shared Caddy or enable systemd timers. Workstation offsite is
disabled by the operator and must remain disabled during this release.

## Foundation release, 13 October 2026

[ID PR #167](https://github.com/updatingspace/id/pull/167) updates account/profile,
security, public documents, 18+ registration and transactional email presentation.
Build source: `56a334f246a4b63f22e0bb9b3f0d9ff02a0a2233`; subsequent validation
commit `431e64dc945a592fdb537e279b7f54c4565d0424` changes only the smoke-test heading.
The YDB TLS/static authentication, S3 endpoint and monitoring access/avatar code
is included in this source: do not apply the historical standalone patches.

Build `Dockerfile.api`, `Dockerfile.web`, and `Dockerfile.jobs` with
`services/id-rust` as context. API, sessions and mutations share the API image;
web and jobs have their own image. The router, flags, secrets, schemas and
schedules are preserved. Four public GET routes are added to web; exact export
and API routes retain their precedence. Monitoring access keeps live ID role
checks and the same current-user avatar allowlist.

Recovery package: `/opt/updspace-id/releases/foundation-56a334f-20261013/`.
It contains source, images, checksums, public verification, `release.json` and a
guarded `rollout.py` with `prepare`, `apply`, `rollback`. `before.json` remains
private on the server. Import `images.tar` and register the exact digest aliases
from `images.json` before applying this overlay to a replacement node.

Deployment uses guarded patches against current object versions; do not replace
the running workload with an unreviewed base manifest. Rollback preserves data
and returns the previous images/router while disabling new registrations: the
old API does not enforce the new adult declaration. Re-enable registration only
with a compatible web/API pair. The previous consent recovery package remains
under `/opt/updspace-id/releases/consent-21f1cd2-20261011/`.

Checks of public pages, complete asset bytes, OIDC discovery/JWKS and anonymous
access refusal are separate from real-account login and external mail delivery.
No credentials are created or rotated by this release. Legal retention/provider
facts and a fresh backup restore remain separate acceptance work.

Client/bootstrap and backup recovery: `docs/id-access.md` at infra root.
