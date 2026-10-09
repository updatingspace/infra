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
patch from the repository root; build `services/id-rust/Dockerfile.api` with
`services/id-rust` as context. Run the policy unit and isolated YDB integration tests
before importing the image and applying this overlay.

Imported image: `docker.io/updspace/id-observability@sha256:c82b7474009486c3cdd8ac16786d06bb1c91ab46254c6182679c29e0309086a5`.
Recovery archive on VM: `/opt/updspace-id/images/profile-20261009.tar`, SHA256
`ecde2f03db0b91c57c24f826bc4246976d7255c28fe66539d79b26fef01e22c0`.
The archive is required on a replacement node; the local image name is not a registry.
Client/bootstrap and backup recovery are described in `docs/id-access.md` at infra root.

The patch also requires the same live role policy on `/oauth/userinfo` for the
`observability` client. Other clients are unchanged. This prevents native GlitchTip
login from bypassing the edge policy after an ID account switch.

Profile scopes now include `preferred_username` from the existing ID username.
The stable subject and consent scope filtering are unchanged; isolated real-YDB
exchange tests cover included and omitted username claims.
