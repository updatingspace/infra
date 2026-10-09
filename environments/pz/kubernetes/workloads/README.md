# PZ, panel and HTTPS workloads

Apply `../platform` first. This root assumes namespaces `zomboid` and `edge`,
default-deny/same-namespace/DNS NetworkPolicies, and k3s ServiceLB with Traefik
disabled. It requires existing secrets `zomboid/pz-runtime`, `zomboid/pz-panel`
and `edge/caddy-runtime`; import the previous container environments without
printing values. Terraform neither reads secret values nor stores them in state.

Before the first apply: save and gracefully stop the Compose game; confirm its
clean exit and back up the stopped world, panel database, Steam files and TLS
data. Disable Compose automatic starts. Preserve original data ownership.
Export the existing images and import them into k3s containerd under unique
migration tags, then set those exact references in private `terraform.tfvars`.
Initial imported pods use `imagePullPolicy: Never`. After a verified panel update,
its official stable `tag@sha256` image uses `IfNotPresent`; see
[panel auto-updates](../panel-updater/README.md).

From the repository root (Terraform plan/apply executes on the VM):

```sh
python3 environments/pz/kubernetes/deploy.py sync
python3 environments/pz/kubernetes/deploy.py plan workloads
python3 environments/pz/kubernetes/deploy.py apply workloads
# Acceptance commands below execute on the VM with operator kubeconfig:
kubectl -n zomboid wait --for=condition=Ready pod/zomboid-0 --timeout=30m
kubectl -n zomboid rollout status deployment/panel
kubectl -n edge rollout status deployment/caddy
kubectl -n zomboid exec zomboid-0 -- python3 /usr/local/bin/pz-game rcon players
```

The game remains a single StatefulSet pod. SIGTERM invokes the existing runtime's
`save`/`quit` handler through localhost RCON, with console input as a fallback;
the runtime then waits for its child process. Kubernetes allows 300 seconds.
Confirm exit code 0 before treating shutdown as clean; pod disappearance alone
is insufficient because the process can be killed when grace expires.
Startup permits 30 minutes
to accommodate the large world and the 2.2 CPU limit.
There is no liveness restart while the game may be saving. `OnDelete` means
Terraform template edits do not restart a running world: schedule maintenance,
back up, and delete the pod normally (never `--force` or `--grace-period=0`) to
apply an image/config update. Panel and Caddy use `Recreate` to avoid concurrent
writes; panel changes do not restart the game.

The `zomboid` namespace reserves **8512 MiB requests / 11136 MiB limits** and
keeps its **2600m CPU** limit. The game retains **2200m / 10 GiB**; panel is
limited to **300m / 768 MiB**, leaving **100m / 128 MiB** for its updater job
(64 MiB memory request). Across application namespaces, memory limits total
**11904 MiB**, leaving **96 MiB** within the 12000 MiB application budget.
The game's JVM uses **Xmx 8 GiB**, leaving room for native memory within its
10 GiB container limit. The panel's CPU and RAM limits apply independently.

The optional [panel telemetry adapter](../panel-telemetry/README.md), enabled
in the Terraform configuration, reports game CPU against its 2.2 CPU limit,
game working-set RAM against 10 GiB, and the game's actual JVM heap separately.
It excludes legacy HOST history and does not substitute host or panel values
when game metrics are unavailable. Its strict source compatibility checks and
the updater health guard reject incompatible future panel images before traffic
switches; this does not add Kubernetes Start/Stop/Restart controls to the panel.

For an offline backup or rollback, scale the StatefulSet to zero and wait for
normal termination. Deleting its pod while replicas remains one creates a new
game immediately. Stop and wait for panel, Caddy and collector writers as well
before taking a consistent backup or handing their data to Compose. The full
[shutdown and rollback sequence](../README.md#обслуживание-и-откат) includes
ServiceLB port release and prevents concurrent Terraform/recovery jobs.

The game template requests **1536 MiB ephemeral storage** and limits it to
**3 GiB**. Panel requests/limits are **128/512 MiB**, updater **16/64 MiB**; together these fit the
namespace's **2/4 GiB** ephemeral requests/limits quotas. This covers temporary
container storage separately from persistent volumes. The initial 1 GiB game
limit caused eviction during startup-backup; deployed PZ classes use Apache's
default `ParallelScatterZipCreator` temporary files. The corrected startup reached
Ready without restarts; the 15-second sampler observed a maximum of 1.317 GiB
allocated in `/tmp/parallelscatter*`, then complete cleanup in the same container.
This is a sampled temporary-file peak, not the absolute peak or whole writable-layer usage. Repeated failed starts rotated the ZIP
history; [the incident and scoped recovery](../README.md#инцидент-первого-запуска-временные-zip-и-история-backup)
are documented without restoring the world itself.

PersistentVolumes reference the existing `/opt/pz-stack/data/*` paths, carry
node affinity, and use `Retain` plus Terraform `prevent_destroy`. Claims share
only within one node; panel binaries are read-only. PV capacities and namespace
PVC quotas account for requested storage; **they do not enforce filesystem
usage**. Filesystem quotas or dedicated bounded filesystems are separate host
configuration. The storage root provides a **26 GiB shared filesystem** for
game binaries, world, Steam, panel data and panel logs, and **256 MiB** for Caddy.
The game `/tmp` limit does not enlarge these filesystems; root SSD space remains
shared by all environments. The root cannot relocate data by changing
`storage_root`.
The owner plans to grow the cloud SSD from 60 to 100 GiB himself. The separate
[26 → 50 GiB storage-growth stage](../storage-growth/README.md) is prepared,
not applied; neither the existing migration state nor these PVC declarations
should be changed to rerun the original data migration.

No Docker socket, hostPath pod mount or host namespace is given to these pods.
Game and Caddy have no service account token. With telemetry enabled, the panel
mounts a projected token for the dedicated `panel-telemetry` ServiceAccount,
allowed only to GET the named `zomboid/zomboid-0` core Pod and Metrics API Pod.
It cannot list resources, read Secret API, exec or modify workloads. The updater
has its own scoped token with the trust limits described in its runbook.
Public ports are game UDP 16261/16262 and HTTPS
TCP 80/443 + UDP 443. RCON, metrics and panel HTTP remain cluster-internal.
The game can reach public Steam peers; panel/Caddy have public HTTP(S) egress.
Those public-egress rules exclude private/link-local ranges; a separate panel
rule permits the Kubernetes Service IP `10.43.0.1:443` and API endpoint
`10.130.0.30:6443` for telemetry. This is a single-node deployment, with
no high availability if that VM or its disk is lost.

For rollback, gracefully stop Kubernetes game and panel before restarting
Compose. Never run both orchestration systems against the world or database.
Reuse the current mounted directories through `/opt/pz-stack/data/*` symlinks;
do not overwrite newer saves with the pre-migration archive or retired source
directories. Back up the current files from their real mount paths, not just the
symlinks. Keep workloads/observability apply paused while Compose owns the data,
and confirm no `svclb-*` pods still claim its public ports before starting it.
