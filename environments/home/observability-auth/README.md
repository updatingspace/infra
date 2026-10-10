# Monitoring sessions in k3s

This component stores encrypted browser sessions in host-only cookies; UpdSpace ID
owns user identity and live authorization. It has one pod, request 50m CPU/64Mi,
limit 250m/128Mi. No Redis or database is added. Measured idle use: 1m CPU/4Mi.
NetworkPolicy admits only shared Caddy and allows egress only to ID and cluster DNS.

`client.json` is the declared OAuth client. `provision-client.py` checks it by
default and refuses mismatching records, duplicate client IDs or a missing recovery
file. `--apply` creates the absent client, persists its private credentials before
the database write, writes an audit record and installs the Kubernetes Secret.
It never edits existing users or roles. The explicit
`--apply --add-application-callbacks` migration accepts only the original five
callbacks or the exact intermediate GlitchTip callback spelling, then installs the
seven declared HTTPS callbacks. It compares the persisted callback list and secret
hash at write time and keeps the same client secret. Other drift is rejected. Initial
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
and Kubernetes `observability-auth/oauth2-proxy` plus `observability/grafana-oidc`.
The existing encrypted ID backup includes the private file and proxy Secret; the
Grafana Secret is recreated from the same private file by `provision-client.py --apply`. Restore namespace and the same client/YDB state, then the private
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

After the gate is ready, apply native Grafana configuration from `../monitoring`
and the GlitchTip provider using `sudo python3 ../glitchtip/configure-oidc.py --apply`
with paths relative to this directory. The GlitchTip provider and organization link
are included in its PostgreSQL backup. No user is created by the provisioning tool;
application accounts are created only when an eligible person completes ID login.

Grafana avatar requests also use this live gate. Only a validated ID response can
supply `X-Observability-Avatar`; incoming copies are stripped and the internal
header never reaches applications or the browser. Shared Caddy redirects the
current user's matching `/avatar/<sha256-email>` to the signed ID picture URL.
Other users, missing pictures and ordinary application requests keep their normal
responses. No new provider scope, endpoint, service, database or role is required.
