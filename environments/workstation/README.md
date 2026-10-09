# Operator workstation: home monitoring DNS

The operator explicitly approved this change only on their current computer.
`hosts-monitoring.block` maps grafana/prometheus/alerts/errors/status.updspace.com
to 192.168.1.176. Public Cloudflare DNS stays proxied and unchanged; other devices
receive no overrides. HTTPS still verifies the original hostname and certificate.

Run `sudo python3 environments/workstation/apply-monitoring-hosts.py` locally.
The tool preserves unrelated hosts entries and saves the original file at
`/etc/hosts.before-updspace-monitoring`. Reapplying an identical block does nothing.
Do not run it on the VM or distribute it to other computers without approval.

Outside the home LAN these five domains require a route/VPN to 192.168.1.176.
For normal external access remove only the BEGIN/END UpdSpace block from
`/etc/hosts`; restoring the entire backup may overwrite unrelated later changes.
ID and Portal themselves are not overridden by this block.
