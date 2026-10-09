\set ON_ERROR_STOP on
BEGIN;
DO $$
DECLARE
    own_schema text := current_user;
    other_schema text;
BEGIN
    IF current_database() <> 'updspace' OR current_schema() <> own_schema THEN
        RAISE EXCEPTION 'Unexpected database or default schema';
    END IF;
    IF has_schema_privilege('public', 'CREATE') THEN
        RAISE EXCEPTION 'Application role can create objects in public';
    END IF;
    FOR other_schema IN
        SELECT nspname FROM pg_namespace
        WHERE (nspname = 'id' OR starts_with(nspname, 'portal_'))
          AND nspname <> own_schema
    LOOP
        IF has_schema_privilege(other_schema, 'USAGE')
           OR has_schema_privilege(other_schema, 'CREATE') THEN
            RAISE EXCEPTION 'Application role can access another service schema';
        END IF;
    END LOOP;
END $$;
CREATE TABLE migration_isolation_check (tenant_id uuid NOT NULL, value text NOT NULL);
INSERT INTO migration_isolation_check VALUES
    ('00000000-0000-0000-0000-000000000001', 'first tenant'),
    ('00000000-0000-0000-0000-000000000002', 'second tenant');
DO $$
BEGIN
    IF (SELECT count(*) FROM migration_isolation_check
        WHERE tenant_id = '00000000-0000-0000-0000-000000000001') <> 1 THEN
        RAISE EXCEPTION 'Tenant-scoped query failed';
    END IF;
END $$;
ROLLBACK;
