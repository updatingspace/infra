# Monitoring sessions in k3s

This component stores encrypted browser sessions in host-only cookies; UpdSpace ID
owns user identity and live authorization. It has one pod, request 50m CPU/64Mi,
limit 250m/128Mi. No Redis or database is added. Measured idle use: 1m CPU/4Mi.
NetworkPolicy admits only shared Caddy and allows egress only to ID and cluster DNS.

`client.json` is the declared OAuth client. `provision-client.py` checks it by
default and refuses mismatching records, duplicate client IDs or a missing recovery
file. `--apply` creates the absent client, persists its private credentials before
the database write, writes an audit record and installs the Kubernetes Secret.
It never edits existing users, roles or an existing client's settings. Initial
root bootstrap was explicitly approved by the operator on 2026-10-09; it is not a
replacement for normal delegated client administration. After an uncertain result,
run check first; do not delete the private recovery file and retry blindly.

Deployment order, from the versioned infra release on the VM:

1. Deploy the ID production overlay with the observability patch and imported image.
2. Render with `python3 environments/home/observability-auth/render.py`; create its
   namespace first, then run the client check and explicit `--apply` as root.
3. Review/server-dry-run and apply the rendered resources; wait for oauth2-proxy ready.
4. Validate shared Caddy and apply the reviewed `scripts/render.py` access diff.
   A checksum change restarts the shared edge briefly; preserve a rollback pair.
5. Apply `uptime-kuma/portal-network.yaml`, then synchronize `config.json` using
   the existing Kuma configuration tool. Pauses and notification bindings persist.

Secrets live at `/opt/updspace-infra/private/observability-oidc.json` (root 0600)
and Kubernetes `observability-auth/oauth2-proxy`. The existing encrypted ID backup
includes both. Restore namespace and the same client/YDB state, then the private
file and Secret; check before apply. Client secrets must never enter Git or shell
arguments. The immutable source archive contains configuration, not credentials.

Checks from infra root:

```sh
CADDY_BINARY=/path/to/caddy python3 -m unittest discover -s environments/home/observability-auth -p 'test_*.py'
python3 scripts/verify-access.py --origin-ip 192.168.1.176
```

The gate test starts isolated local HTTP fixtures and the real Caddy binary.
Live read-only verification reaches ID login without a personal session; optional
`--cookie-file` checks Grafana assets with an operator-provided private cookie file.
It never logs cookies or state. See `docs/id-access.md` for policy and limitations.
