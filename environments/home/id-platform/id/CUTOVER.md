# Final ID cutover (prepared, not applied)

The public ID still uses YC. Local ID contains a trial copy and synthetic auth
fixtures. All five local CronJobs remain suspended. Do not run the trial auth
fixture against the final restored database. This procedure remains pending the
user's decision about the unsupported file-backed YDB configuration described in
the parent README. It does not authorize cloud data deletion.

## Refresh the source and prepare rollback

Run on the workstation, with its existing YC identity. Both scripts are read-only
toward YC. The output directories must be new and private; the source snapshot
contains runtime configuration and must not be committed.

```sh
python3 snapshot_source.py /tmp/id-source-before-cutover
python3 prepare_cutover.py /tmp/id-source-before-cutover /tmp/id-cutover-bundle
python3 -m unittest discover -s . -p 'test_*.py' -v
```

The generator refuses changed active revisions, additional ID containers or
triggers, legacy functions, changed database bindings, unknown gateway backends,
and unexpected inherited runtime-account roles. It preserves the original
gateway specification and produces maintenance responses for all 131 operations
across 81 paths. The commands are arrays in `plan.json`, not an auto-executing
script. Paths in those commands are relative to the private output directory.

The observed source has five containers and five timers; no ID Cloud Functions.
One `ydb.editor` binding on database `etnq1cp5vubgd8ono4t4` belongs to runtime
account `aje5cbnkoueu47koscvk`. This account's observed folder-level role is only
`container-registry.images.puller`. Other accounts have inherited invocation or
administrative rights. Removing container-local bindings alone is insufficient.
Keep shared folder permissions intact and coordinate any deployment/manual
writers before freezing ID. Refresh these observations immediately before use.

## Freeze the source

1. Coordinate the shared edge owner and Portal agent. Preserve the current source
   DNS record and fresh shared Caddy configuration. Keep local public access
   closed, local jobs suspended, and the daily backup timer disabled during import.
2. Execute only `pause_previously_active_triggers`, then `gateway_maintenance` and
   `remove_direct_invocation_bindings` from the prepared plan. Reread each resource
   after the mutation. If a result is ambiguous, inspect it rather than retrying.
3. Verify the cloud gateway returns 503 for root, login, API and OIDC paths. Verify
   paused timer status and the removed direct invocation bindings. Let existing
   invocations drain: the largest source timeout is **600 seconds**. The migration
   maintenance window therefore includes at least ten minutes plus copy/check time.
4. Execute `fence_database_runtime` for this ID database only. Reread the database
   ACL and verify that the three API containers and jobs can no longer access YDB
   (authenticated direct readiness probes must fail). IAM propagation can lag;
   absence of a binding alone is not runtime proof. If any source writer remains
   possible, keep maintenance active and do not take the final snapshot yet.
5. Take the final ordered YDB dump with database-level consistency. Take a fresh
   ID-only S3 copy with before/after inventory and readback hashes. Encrypt both
   with the existing backup recipient and verify their copies on the workstation.
   The earlier `20261009T114911Z` dump is trial evidence, not this final snapshot.

API Gateway maintenance uses the documented
[static response integration](https://yandex.cloud/en/docs/api-gateway/concepts/extensions/dummy).
Invocation rights follow [YC access management](https://yandex.cloud/en/docs/serverless-containers/security/).
Administrative/operator access deliberately remains available for backup/rollback.

## Replace the trial and switch traffic

1. Scale local ID to zero and wait for its pod and any jobs to stop. Retain a full
   encrypted trial backup. Restore all 67 source tables with the bundled YDB CLI
   `tools restore --replace --restore-acl 0 --replace-sys-acl 0`; preserve local
   database users, TLS and grants. Use the final dump, not the trial dump. Check the
   complete table inventory, schema/indexes, data hashes/counts and TTL effects.
   Replacement must eliminate synthetic user `-2100000000` and its outbox records.
   A merge/upsert restore into trial data is not sufficient.
2. Reconcile only ID's two Garage buckets against the final source manifest;
   preserve Portal's bucket/key. Verify every copied object's length/hash/metadata.
   Remove trial-only extras only after listing and checking the exact keys.
3. Start ID with jobs still suspended. Verify all containers, dependency readiness,
   unchanged JWKS/discovery, original secret continuity and absence of synthetic
   accounts/outboxes. Remove the local `TRIAL_ONLY` marker after successful checks.
   Preserve the durable local YDB acceptance marker if it is outside source tables.
4. Apply shared-edge changes through a new central infra revision, never by editing
   the immutable `/opt/updspace-infra/source` release. First serve an ID maintenance
   response on the VM using the existing valid certificate. Change only the ID DNS
   record to the VM route; preserve its intended Cloudflare proxy setting.
5. Transfer ID TLS to the shared Caddy's automatic ACME management while the origin
   remains in maintenance. Confirm a valid managed certificate before opening the
   route. The trial's manually issued certificate expires on 2027-01-07 and does
   not renew itself. Preserve all unrelated edge routes and monitoring access.
6. Open the ID reverse proxy, verify public HTTPS, auth/session behavior and Portal
   OIDC integration, then enable the five local schedules and daily backup timer.
   Verify a completed encrypted backup is copied to the workstation. The measured
   daily cold backup interrupts ID for about 18 seconds; that is separate from the
   longer one-time migration maintenance window.

## Rollback boundary

Before the target accepts any write (including a job or auth test), keep local
writers stopped, restore the source DB binding, container bindings and original
gateway from the prepared plan, verify source readiness, restore source DNS and
resume only timers that were previously active. Keep the VM route closed while
clients may still resolve it. Never resume previously paused timers.

After the first target write, do not just change DNS back: passwords, sessions,
MFA counters, consents and outboxes may already differ. Freeze both sides and
perform reviewed reverse synchronization or restore to the selected authoritative
state. Keep cloud data/resources for this recovery boundary; do not delete them.

## Recovery without the YC image registry

Application manifests use pinned local images with `imagePullPolicy: Never`.
Daily data/secret backups alone cannot rebuild a lost node. A separate encrypted
OCI archive on the workstation includes the five pinned linux/amd64 images:
runtime, unchanged web, internal Caddy, YDB and Garage.

Archive: `~/.local/share/updspace-backups/id-images/20261009T133305Z/images.oci.tar.gpg`.
Ciphertext SHA256: `d0090b52dc6fd15e4556bf52e044bc18659f342f440a14e9cbc6a25270f9164c`.
Verify `COMMITTED`/`manifest.json`, decrypt with the existing local backup key, then:

```sh
python3 verify_image_archive.py images.oci.tar manifest.json
sudo k3s ctr images import --platform linux/amd64 images.oci.tar
```

The verifier checks every blob hash and the complete linux/amd64 dependency graph
for all five pinned references. A real isolated containerd import was also checked
without registry access. This is image recovery evidence, not a fresh-node end-to-end
cluster rebuild. Back up a new image archive whenever a deployed image digest changes.
Shared edge/k3s bootstrap and shared Garage recovery remain centrally owned.
