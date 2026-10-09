# Existing node configuration

This is adoption of `updspace-home`, not a destructive OS installer. k3s is pinned
at v1.36.4+k3s1; actual kubelet reservations, eviction thresholds, local networking,
firewall rules and mount dependencies are versioned. Cluster tokens, kubeconfig,
SSH keys and backup credentials stay outside Git. Standard Ubuntu units/packages
are provided by the OS; this repository records only service-specific overrides.

Review with Ansible Core (standard builtin modules only):

```sh
ansible-playbook -i inventory.ini playbook.yaml --check --diff
```

An explicit apply without `--check` only installs these configuration files with
rollback copies. It does NOT restart k3s/firewall, format storage, reboot the host,
or change the rescue boot selection. Activation must be coordinated with users.
The stored firewall rules preserve existing legacy rules; they are not a security
audit or permission to remove them.

`storage.json` records the three existing ext4 loop files, UUIDs, mounts, fstab
lines and symlinks. Restore/adopt those files from backup before bootstrapping
workloads. Never format an existing filesystem. Preserve `/srv/pz-volumes/data.ext4`
(64 GiB), `spool.ext4` (64 GiB), `edge.ext4` (256 MiB), their UUIDs and ownership.
The game/spool fstab lines are supplied in `storage.json`; merge only those lines
on a replacement node, do not overwrite unrelated fstab entries. The shared-edge
mount unit and k3s dependencies are installable through this playbook.

PZ backup/updater host units retain their own CPUQuota/MemoryMax from the existing
PZ installers. They are host maintenance jobs, not Docker application services.
All newly deployed applications run in k3s. The historical TeamSpeak MariaDB was moved to k3s; see `../teamspeak/README.md`.
`docs/inventory/legacy-teamspeak.json` retains the pre-migration inventory.
Ansible Core 2.19.3 syntax/check passed: changed=0. A fresh-node restore and
an Ansible apply have not been performed in this task.
