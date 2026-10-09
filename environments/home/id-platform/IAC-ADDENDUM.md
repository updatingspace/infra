# Applicable configuration addendum

This adds files to the frozen 28-file handoff without changing its hashes.
Copy the files listed in `IAC-ADDENDUM.sha256` into the same relative locations.

## Garage

Run on the VM as root, with the versioned directory containing the existing
`garage/desired-state.json` and `garage/bucket-settings.json`:

```sh
python3 garage/reconcile.py
python3 garage/reconcile.py --apply
```

Default mode is read-only and exits 2 for CORS/lifecycle drift. Both modes audit
layout/version/staged changes, region/replication, key names/permissions and
bucket ownership before proceeding. Unknown ownership/layout changes fail closed.
`--apply` can invoke only Garage `UpdateBucket` for the CORS/lifecycle fields in
the desired file. It does not create/rotate keys, reassign layout, grant rights,
create/delete buckets or directly delete objects. Lifecycle expiration itself
retains the semantics of the configured rules, so review desired changes first.
Each changed setting is reread before mutation and verified afterwards; ambiguous
results are not automatically retried. Existing settings produce no write calls.

It uses `/garage json-api` through `k3s kubectl exec`, with no exported admin token
or runtime secret. Request types were checked against Garage v2.4.1 source and
the official v2.4 API schema. CORS converts the AWS plural field names to the
singular XML-compatible admin representation without silently dropping fields.
The layout capacity in the original desired file follows the CLI's rounded GiB
display; the audit compares it at the same precision.

`python3 garage/test_reconcile.py`: three tests passed, covering read-only default,
idempotent/field-scoped apply, unknown CORS fields and fail-closed owner/layout drift.
Real VM read-only audit passed with no changes. Live `--apply` was not run.

## YDB

```sh
python3 id/install-ydb-config.py
python3 id/install-ydb-config.py --apply
```

The installer reads the adjacent versioned `id/ydb-config.yaml` and targets
`/srv/updspace/id-ydb/cluster/kikimr_configs/config.yaml`. Default mode only checks
equality. It rejects missing authenticated-volume markers, anonymous/builtin auth
settings and bootstrap passwords. If a change is requested, it requires the
StatefulSet to have zero replicas **and no remaining YDB pods**, then atomically
installs the config with UID/GID65534 and mode0600. It does not stop/start workloads
or format PDisk; shutdown/backup/restart remain deliberate operator steps.
Real VM read-only check passed: config already matches. No live apply occurred.
Full bootstrap of a new empty YDB volume remains a separate recorded limitation.

Checked copies on VM: `/opt/updspace-id/iac-audit/{garage,id}`.
