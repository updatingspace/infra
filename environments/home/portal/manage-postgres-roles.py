#!/usr/bin/env python3
"""Check declarative PostgreSQL role isolation; apply only with an explicit flag."""

import argparse
import json
import os
from pathlib import Path
import re
import stat
import subprocess


def load_config(path: Path) -> dict:
    config = json.loads(path.read_text())
    if set(config) != {"database", "roles"} or config["database"] != "updspace":
        raise ValueError("Expected the updspace database and a roles list")
    roles = config["roles"]
    if not isinstance(roles, list) or not roles or any(
        not isinstance(role, str)
        or not re.fullmatch(r"(?:id|portal_[a-z][a-z0-9_]{0,55})", role)
        for role in roles
    ) or len(set(roles)) != len(roles):
        raise ValueError("Expected unique id/portal_* application roles")
    return config


def literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def audit_sql(config: dict) -> str:
    names = ", ".join("(" + literal(role) + ")" for role in config["roles"])
    return f"""
WITH desired(name) AS (VALUES {names}),
db AS (SELECT * FROM pg_database WHERE datname = current_database()),
roles AS (SELECT r.* FROM pg_roles r JOIN desired d ON r.rolname = d.name),
schemas AS (
  SELECT n.*, pg_get_userbyid(nspowner) AS owner,
    EXISTS (SELECT FROM aclexplode(COALESCE(nspacl, acldefault('n', nspowner)))
            WHERE grantee = 0) AS public_acl
  FROM pg_namespace n
  WHERE nspname !~ '^pg_' AND nspname <> 'information_schema'
),
blockers(message) AS (
  SELECT 'role membership requires review: ' || r.rolname FROM roles r
    WHERE EXISTS (SELECT FROM pg_auth_members m
                  WHERE m.member = r.oid OR m.roleid = r.oid)
  UNION ALL
  SELECT 'application role owns database: ' || r.rolname FROM roles r, db
    WHERE db.datdba = r.oid
  UNION ALL
  SELECT 'unexpected schema owner: ' || s.nspname FROM schemas s
    JOIN desired d ON d.name = s.nspname WHERE s.owner <> d.name
  UNION ALL
  SELECT 'unmanaged schema access requires review: ' || r.rolname || '/' || s.nspname
    FROM roles r CROSS JOIN schemas s
    WHERE s.nspname <> 'public' AND s.nspname NOT IN (SELECT name FROM desired)
      AND (has_schema_privilege(r.oid, s.oid, 'USAGE')
           OR has_schema_privilege(r.oid, s.oid, 'CREATE'))
),
issues(message) AS (
  SELECT message FROM blockers
  UNION ALL
  SELECT 'missing role: ' || d.name FROM desired d
    WHERE NOT EXISTS (SELECT FROM roles r WHERE r.rolname = d.name)
  UNION ALL
  SELECT 'missing schema: ' || d.name FROM desired d
    WHERE NOT EXISTS (SELECT FROM schemas s WHERE s.nspname = d.name)
  UNION ALL
  SELECT 'unsafe role attributes: ' || r.rolname FROM roles r
    WHERE NOT r.rolcanlogin OR r.rolsuper OR r.rolcreatedb OR r.rolcreaterole
       OR r.rolreplication OR r.rolbypassrls
  UNION ALL
  SELECT 'database grants differ: ' || r.rolname FROM roles r, db
    WHERE NOT has_database_privilege(r.oid, db.oid, 'CONNECT')
       OR has_database_privilege(r.oid, db.oid, 'CREATE')
       OR has_database_privilege(r.oid, db.oid, 'TEMP')
  UNION ALL
  SELECT 'public database grants' FROM db
    WHERE EXISTS (SELECT FROM aclexplode(COALESCE(datacl, acldefault('d', datdba)))
                  WHERE grantee = 0)
  UNION ALL
  SELECT 'public schema grants: ' || s.nspname FROM schemas s
    WHERE s.public_acl AND (s.nspname = 'public' OR s.nspname IN (SELECT name FROM desired))
  UNION ALL
  SELECT 'schema grants differ: ' || r.rolname || '/' || s.nspname
    FROM roles r CROSS JOIN schemas s
    WHERE (s.nspname = r.rolname AND
           (NOT has_schema_privilege(r.oid, s.oid, 'USAGE')
            OR NOT has_schema_privilege(r.oid, s.oid, 'CREATE')))
       OR (s.nspname <> r.rolname AND
           (has_schema_privilege(r.oid, s.oid, 'USAGE')
            OR has_schema_privilege(r.oid, s.oid, 'CREATE')))
  UNION ALL
  SELECT 'search_path differs: ' || r.rolname FROM roles r, db
    WHERE NOT EXISTS (
      SELECT FROM pg_db_role_setting setting, unnest(setting.setconfig) AS item
      WHERE setting.setrole = r.oid AND setting.setdatabase = db.oid
        AND item = 'search_path=' || r.rolname || ', pg_catalog')
)
SELECT jsonb_build_object(
  'existing_roles', COALESCE((SELECT jsonb_agg(rolname ORDER BY rolname) FROM roles), '[]'::jsonb),
  'blockers', COALESCE((SELECT jsonb_agg(message ORDER BY message) FROM blockers), '[]'::jsonb),
  'issues', COALESCE((SELECT jsonb_agg(message ORDER BY message) FROM issues), '[]'::jsonb))
""".strip()


def load_passwords(path: Path | None, missing: list[str]) -> dict:
    if not missing:
        return {}
    if path is None:
        raise ValueError("New roles require an external --credentials JSON file")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd) as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.geteuid():
            raise ValueError("Credentials must be a private regular file owned by the operator")
        values = json.load(source)
    if not isinstance(values, dict) or any(
        not isinstance(values.get(role), str) or len(values[role]) < 16
        or any(ord(char) < 32 for char in values[role])
        for role in missing
    ):
        raise ValueError("Every new role needs an external password of at least 16 characters")
    return {role: values[role] for role in missing}


def apply_sql(config: dict, state: dict, passwords: dict) -> str:
    if state["blockers"]:
        raise ValueError("Resolve audit blockers before applying role changes")
    roles = config["roles"]
    missing = set(roles) - set(state["existing_roles"])
    if set(passwords) != missing:
        raise ValueError("Credentials must cover exactly the new roles")
    sql = [
        "BEGIN;", "SET LOCAL standard_conforming_strings = on;",
        "SET LOCAL log_statement = 'none';", "SET LOCAL log_min_error_statement = 'panic';",
        "SELECT pg_advisory_xact_lock(hashtextextended('updspace-role-management', 0));",
        "REVOKE ALL ON DATABASE updspace FROM PUBLIC;",
        "REVOKE ALL ON SCHEMA public FROM PUBLIC;",
    ]
    for role in roles:
        if role in missing:
            sql.append(f"CREATE ROLE {role} LOGIN PASSWORD {literal(passwords[role])};")
        sql.extend([
            f"ALTER ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;",
            f"CREATE SCHEMA IF NOT EXISTS {role} AUTHORIZATION {role};",
            f"REVOKE ALL ON SCHEMA {role} FROM PUBLIC;",
            f"REVOKE ALL ON DATABASE updspace FROM {role};",
            f"GRANT CONNECT ON DATABASE updspace TO {role};",
            f"GRANT USAGE, CREATE ON SCHEMA {role} TO {role};",
            f"ALTER ROLE {role} IN DATABASE updspace SET search_path = {role}, pg_catalog;",
        ])
    for role in roles:
        foreign = ", ".join(["public"] + [other for other in roles if other != role])
        sql.append(f"REVOKE ALL ON SCHEMA {foreign} FROM {role};")
    # A concurrent ownership/ACL change must roll back the entire application.
    sql.append("DO $postcheck$ DECLARE result jsonb; BEGIN result := (" + audit_sql(config) + """
); IF jsonb_array_length(result->'issues') <> 0 THEN
RAISE EXCEPTION 'Role isolation postcondition failed'; END IF; END $postcheck$;
COMMIT;
""")
    return "\n".join(sql)


def run_sql(sql: str, pod: str) -> str:
    command = ["k3s", "kubectl", "-n", "updspace-data", "exec", "-i", pod, "--",
               "psql", "-X", "-qAt", "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-d", "updspace"]
    result = subprocess.run(command, input=sql, text=True, capture_output=True, timeout=120)
    if result.returncode:
        # Server/psql errors can contain SQL, including bootstrap passwords.
        raise RuntimeError("PostgreSQL operation failed; captured SQL/output is not printed")
    return result.stdout.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Read-only audit (default)")
    mode.add_argument("--apply", action="store_true", help="Reconcile roles in one transaction")
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("postgres-roles.json"))
    parser.add_argument("--credentials", type=Path, help="Private external JSON for new roles only")
    parser.add_argument("--pod", default="postgres-0", help="PostgreSQL pod in updspace-data")
    args = parser.parse_args(argv)
    config = load_config(args.config)
    state = json.loads(run_sql("BEGIN READ ONLY;\n" + audit_sql(config) + ";\nCOMMIT;", args.pod))
    if args.apply and state["issues"]:
        missing = [role for role in config["roles"] if role not in state["existing_roles"]]
        if state["blockers"]:
            print(json.dumps(state, indent=2))
            raise ValueError("Resolve audit blockers before applying role changes")
        passwords = load_passwords(args.credentials, missing)
        run_sql(apply_sql(config, state, passwords), args.pod)
        state = json.loads(run_sql("BEGIN READ ONLY;\n" + audit_sql(config) + ";\nCOMMIT;", args.pod))
    print(json.dumps(state, indent=2))
    return int(bool(state["issues"]))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        # Do not print exception details from parsing secret JSON or process input.
        raise SystemExit(f"Role management stopped ({type(error).__name__}); no secrets printed") from None
