#!/usr/bin/env python3
"""Test role reconciliation in a disposable, loopback-only PostgreSQL pod on the VM."""

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import secrets
import subprocess
import tempfile


spec = importlib.util.spec_from_file_location(
    "role_manager", Path(__file__).with_name("manage-postgres-roles.py")
)
manager = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manager)
KUBECTL = ["k3s", "kubectl", "-n", "updspace-data"]


def main() -> None:
    config = manager.load_config(Path(__file__).with_name("postgres-roles.json"))
    pod = "postgres-roles-check-" + secrets.token_hex(4)
    image = subprocess.check_output(
        KUBECTL + ["get", "statefulset", "postgres", "-o", "jsonpath={.spec.template.spec.containers[0].image}"],
        text=True, timeout=30,
    )
    if not image.startswith("docker.io/library/postgres:") or "@sha256:" not in image:
        raise ValueError("Expected the digest-pinned deployed PostgreSQL image")
    manifest = {
        "apiVersion": "v1", "kind": "Pod",
        "metadata": {"name": pod, "namespace": "updspace-data", "labels": {
            "app.kubernetes.io/name": "postgres-roles-check"}},
        "spec": {
            "automountServiceAccountToken": False, "restartPolicy": "Never",
            "securityContext": {"runAsUser": 999, "runAsGroup": 999, "fsGroup": 999,
                                "runAsNonRoot": True, "seccompProfile": {"type": "RuntimeDefault"}},
            "containers": [{
                "name": "postgres", "image": image, "imagePullPolicy": "IfNotPresent",
                "args": ["-c", "listen_addresses=127.0.0.1"],
                "env": [{"name": "POSTGRES_DB", "value": "updspace"},
                        {"name": "POSTGRES_HOST_AUTH_METHOD", "value": "trust"}],
                "resources": {"requests": {"cpu": "100m", "memory": "128Mi"},
                              "limits": {"cpu": "500m", "memory": "512Mi"}},
                "securityContext": {"allowPrivilegeEscalation": False,
                                    "capabilities": {"drop": ["ALL"]}},
                "readinessProbe": {"exec": {"command": [
                    "pg_isready", "-h", "127.0.0.1", "-U", "postgres", "-d", "updspace"]},
                    "periodSeconds": 2},
                "volumeMounts": [{"name": "data", "mountPath": "/var/lib/postgresql"},
                                 {"name": "runtime", "mountPath": "/var/run/postgresql"}],
            }],
            "volumes": [{"name": "data", "emptyDir": {"sizeLimit": "256Mi"}},
                        {"name": "runtime", "emptyDir": {}}],
        },
    }
    subprocess.run(KUBECTL + ["create", "-f", "-"], input=json.dumps(manifest),
                   text=True, check=True, timeout=30)
    try:
        subprocess.run(KUBECTL + ["wait", "--for=condition=Ready", "pod/" + pod, "--timeout=60s"],
                       check=True, timeout=65)

        def sql(query: str) -> str:
            return manager.run_sql(query, pod)

        def invoke(*args: str) -> int:
            with contextlib.redirect_stdout(io.StringIO()):
                return manager.main(["--pod", pod, *args])

        assert invoke() == 1
        try:
            invoke("--apply")
        except ValueError:
            pass
        else:
            raise AssertionError("Bootstrap succeeded without external credentials")
        state = json.loads(sql(manager.audit_sql(config)))
        assert not state["existing_roles"]
        with tempfile.TemporaryDirectory() as directory:
            credentials = Path(directory) / "synthetic-credentials.json"
            credentials.write_text(json.dumps({name: secrets.token_hex(32) for name in config["roles"]}))
            credentials.chmod(0o600)
            assert invoke("--apply", "--credentials", str(credentials)) == 0
        assert invoke() == 0
        role_names = ",".join(manager.literal(name) for name in config["roles"])
        fingerprint = f"SELECT md5(string_agg(rolname || ':' || rolpassword, ',' ORDER BY rolname)) FROM pg_authid WHERE rolname IN ({role_names});"
        before = sql(fingerprint)
        sql("SET ROLE portal_bff; CREATE TABLE role_check_marker(value int); INSERT INTO role_check_marker VALUES (42);")
        assert invoke("--apply") == 0
        assert sql(fingerprint) == before
        sql("ALTER ROLE portal_bff CREATEDB; GRANT USAGE ON SCHEMA portal_core TO portal_bff; "
            "ALTER ROLE portal_bff IN DATABASE updspace SET search_path = public; "
            "GRANT CREATE ON DATABASE updspace TO PUBLIC;")
        assert invoke() == 1
        assert invoke("--apply") == 0
        assert sql(fingerprint) == before
        assert sql("SET ROLE portal_bff; SELECT value FROM role_check_marker;") == "42"

        for change, undo in (
            ("GRANT portal_core TO portal_bff;", "REVOKE portal_core FROM portal_bff;"),
            ("ALTER SCHEMA portal_core OWNER TO postgres;", "ALTER SCHEMA portal_core OWNER TO portal_core;"),
        ):
            sql(change)
            try:
                invoke("--apply")
            except ValueError:
                pass
            else:
                raise AssertionError("Unexpected ownership/membership was silently changed")
            assert sql(fingerprint) == before
            sql(undo)

        clean_state = json.loads(sql(manager.audit_sql(config)))
        assert not clean_state["issues"]
        statement = manager.apply_sql(config, clean_state, {})
        sql("ALTER ROLE portal_bff CREATEDB; GRANT portal_core TO portal_bff;")
        try:
            sql(statement)
        except RuntimeError:
            pass
        else:
            raise AssertionError("Concurrent membership drift was committed")
        assert sql("SELECT rolcreatedb FROM pg_roles WHERE rolname='portal_bff';") == "t"
        assert sql(fingerprint) == before
        print("PASS bootstrap, read-only audit, idempotence, drift repair, password/data preservation, blockers and transaction rollback")
    finally:
        subprocess.run(KUBECTL + ["delete", "pod", pod, "--wait=true", "--timeout=60s"],
                       check=True, timeout=65)


if __name__ == "__main__":
    main()
