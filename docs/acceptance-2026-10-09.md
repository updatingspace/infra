# Publication acceptance — 2026-10-09

This records the checks for importing the home k3s configuration into
`updatingspace/infra`. Publication itself does not deploy, restart services,
change DNS or remove cloud resources.

## Verified

- CI run [37937820840](https://github.com/updatingspace/infra/actions/runs/37937820840)
  on `a9ba5b244c3856780cbdd551d44cab3f49dc4f4d`: all five jobs passed.
  Python: 448 tests, including the real age/zstd/rsync archive roundtrip and
  Java regression. Node: 17 passed; the optional local upstream-source fixture
  was absent and its one test skipped. Terraform init/validate/mock-provider
  tests passed for platform, workloads, observability and backup-cloud.
- Read-only `verify-game.py` on `updspace-home`: game readiness, RCON `players`,
  game metrics, panel health, retained PVC mounts and direct-origin HTTPS for
  `pz-admin.updspace.com` passed. Game, panel and Caddy reported zero restarts.
- Public PZ dashboard loaded in the browser using the existing authenticated
  session. It displayed the running server, RCON ready and live JVM/CPU/RAM
  telemetry; browser warning/error collection was empty. No game controls were
  activated during this check.
- `game-config/configure.py --check`: all declared INI/Lua configuration matches
  the VM; no files or managed INI keys differ. `Public=false` remains declared.
- All ten files in `environments/home/host/files.json` matched the VM by SHA256.
- Rendered shared Caddy ConfigMap/Deployment, monitoring Deployments and the two
  monitoring access NetworkPolicies match the live objects. Kubernetes normalizes
  Prometheus `1024Mi` to `1Gi` and omits Grafana's empty `args` array.

## Limits

The Grafana browser works directly against the local origin. Public JavaScript
loading through Cloudflare is still failing from the tested client network;
HTTP 200 for its HTML is not UI acceptance. The monitoring task owns that network
investigation. Cloudflare proxy remains enabled and no speculative Caddy transport
change is included. This source publication does not resolve that access issue.

This pass did not join PZ with a game client or perform a fresh whole-host restore.
Prior data-copy and restore evidence remains in the component handoffs; it is not
replaced by CI. ID and Portal retain the deployment/trial boundaries documented
in their component directories. The running Minecraft recovery helper VM is not
retired by this publication.
