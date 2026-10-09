# Shared local edge

One Caddy deployment in namespace `edge` owns local TCP 80/443 and UDP 443.
Game UDP 16261/16262 uses a separate ServiceLB service.

- `pz-admin.updspace.com` routes to `panel.zomboid:3001`.
- `status.updspace.com` routes to `uptime-kuma.uptime-kuma:3001`.

The local workloads variables select `caddy_config_path = "../local-edge/Caddyfile"`
and `edge_storage_root = "/srv/edge-caddy"`. The default cloud layout is unchanged.
Apply `network.yaml` for the edge egress rule; the Kuma deployment owns its
matching ingress policy.

Persistent `caddy-data` and `caddy-config` live on a separate 256 MiB ext4 loop
filesystem, `/srv/pz-volumes/edge.ext4`, mounted at `/srv/edge-caddy`.
The systemd mount unit is named by `systemd-escape --path --suffix=mount
/srv/edge-caddy`; `k3s.service.d/shared-edge.conf` requires this mount at boot.
Keep this storage and the shared proxy running independently of game backups.
The migration's unchanged source copy remains at `/srv/pz-storage/edge`.

Use the exact imported Caddy image to validate configuration before applying.
For local acceptance, `verify-game.py` needs `--expected-node updspace-home
--edge-storage-root /srv/edge-caddy`. It still requires retained, bound PVCs,
the exact expected paths and an actually mounted filesystem.

Create or change only explicitly selected DNS records. Do not change wildcard,
ID or Portal records. A new hostname starts DNS-only for ACME bootstrap;
verify the origin certificate with correct SNI before enabling Cloudflare proxying.
Coordinate Caddy edits in this repository so parallel services do not overwrite
each other's host routes or compete for 80/443.
