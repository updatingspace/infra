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

The 2026-10-11 consent release updates only mutations and web. API, sessions,
jobs and router retain their existing digests. Its application source is ID commit
`21f1cd25047a13ddca1c991cc026a4ab0db904a3`; validation commit `bafd28e16294d77a525a955492cc933815509d5c`
adds only the shared browser fixture update. See [ID PR #161](https://github.com/updatingspace/id/pull/161).

Build from a clean checkout: apply `../../runtime-local-services.patch`,
`../../observability-access.patch`, then `../../observability-avatar.patch` from
the repository root. Build `services/id-rust/Dockerfile.api` and `Dockerfile.web`
with `services/id-rust` as context. The existing staff, avatar and local storage
patches are preserved. This release changes no database schema or secrets.
ETU requires email and profile; a prior grant missing either must be revoked and
issued again. Other saved grants are confirmed without editing their scopes.

Recovery package on the VM: `/opt/updspace-id/releases/consent-21f1cd2-20261011/`.
It contains the exact source, production patches, images, checksums, verification
record and guarded `rollout.py` (`prepare`, `apply`, `rollback`).
`images.tar` SHA256: `c6ced03c9a13ca86cb462752c13f13176630ac34a4b2b6425b0505aafdb0c280`.
The local image names are not registries; restore this archive on replacement
nodes and register both exact digest aliases before applying the overlay:

```sh
sudo k3s ctr images import /opt/updspace-id/releases/consent-21f1cd2-20261011/images.tar
sudo k3s ctr images tag docker.io/updspace/id-consent-api:20261011 docker.io/updspace/id-consent-api@sha256:b827552cf4e5bce8a7d994e76f4603538561c61d97d0479f68b232345f6b0626
sudo k3s ctr images tag docker.io/updspace/id-consent-web:20261011 docker.io/updspace/id-consent-web@sha256:d045657b66b8c6975429e2844716ee068a2b6541fc2082c70dc123d56b385fd1
```

The previous mutations (`beb855c4...`) and web (`08db40c0...`) images remain
installed. `rollout.py rollback` restores only these two exact images and refuses
to overwrite a later release. Full previous digests are in `release.json`.

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
