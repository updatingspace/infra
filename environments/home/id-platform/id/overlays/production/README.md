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
root-only backup recipient/config; enable the workstation offsite timer separately.
Daily backup pauses ID briefly and restores the captured schedules/replica count.
See `../../CUTOVER.md` for current state, evidence and rollback restrictions.
