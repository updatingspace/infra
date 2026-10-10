# Running ID on the home VM

This is the accepted state after the 2026-10-09 cloud cutover: one ID deployment
and all five existing CronJobs enabled. Restore YDB, ID secrets and Garage objects
and import the pinned images before applying it to a replacement node.

```sh
sudo k3s kubectl kustomize . > /tmp/id-production.yaml
sudo k3s kubectl apply --dry-run=server -f /tmp/id-production.yaml
sudo k3s kubectl diff -f /tmp/id-production.yaml
```

Review the diff before apply. This overlay does not install secrets, import data,
change DNS, modify the shared Caddy ConfigMap or enable systemd timers. Install
`id/updspace-id-backup.{service,timer}` and enable the timer after restoring its
root-only backup recipient/config. Workstation offsite was disabled by operator
request; do not re-enable it during this deployment.
Daily backup pauses ID briefly and restores the captured schedules/replica count.
See `../../CUTOVER.md` for current state, evidence and rollback restrictions.

The mutations image now includes `../../observability-access.patch`, built over
ID source `5bfa8dad` plus the existing `runtime-local-services.patch`. Only mutations
changes; API, sessions, jobs, web and router keep their previous digests.
Build from a clean checkout: apply the runtime patch first, then the observability
patch from the repository root, followed by `observability-avatar.patch`; build `services/id-rust/Dockerfile.api` with
`services/id-rust` as context. Run the policy unit and isolated YDB integration tests
before importing the image and applying this overlay.

Imported image: `docker.io/updspace/id-observability@sha256:beb855c41fd3e1254354404660237c0a41278103fafc09e3f4b4c1e38843f014`.
Recovery archive on VM: `/opt/updspace-id/images/avatar-20261010.tar`, SHA256
`6c2fe954874beb8c46597bd084a3974723426cdc27e30181bc8136cfd5603185`.
The archive is required on a replacement node; the local image name is not a registry.
Import and register the exact digest alias before applying the overlay (`Never`
pull policy does not resolve the imported tag automatically):

```sh
sudo k3s ctr images import /opt/updspace-id/images/avatar-20261010.tar
sudo k3s ctr images tag docker.io/updspace/id-observability:avatar-20261010 docker.io/updspace/id-observability@sha256:beb855c41fd3e1254354404660237c0a41278103fafc09e3f4b4c1e38843f014
```

Client/bootstrap and backup recovery are described in `docs/id-access.md` at infra root.

The patch also requires the same live role policy on `/oauth/userinfo` for the
`observability` client. Other clients are unchanged. This prevents native GlitchTip
login from bypassing the edge policy after an ID account switch.

Profile scopes now include `preferred_username` from the existing ID username.
The stable subject and consent scope filtering are unchanged; isolated real-YDB
exchange tests cover included and omitted username claims.

The avatar patch extends the existing staff authorization response only for a
Grafana request for the current user's SHA-256 email avatar key. It passes a
validated signed picture URL in an internal header; the shared edge consumes and
removes that header. URLs outside the existing HTTPS media bucket are rejected.
All original UserInfo role, scope and token checks remain in place. The patch has
focused Rust tests; the real Grafana and Caddy integration tests cover the URL
format, forged headers, absent sessions, denied roles and fallback behavior.
