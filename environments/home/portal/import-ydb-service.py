#!/usr/bin/env python3
"""Replace one dormant service's trial data; input and output are JSON on stdio."""
import datetime
import json
import os
import sys
import uuid

from portal_ydb_codec import digest, normalize


def prepare_value(value, field, connection):
    db_type = field.db_type(connection)
    if db_type == 'jsonb':
        return connection.ops.adapt_json_value(value, None)
    if value is None:
        return None
    if db_type == 'uuid':
        return uuid.UUID(value)
    if db_type in ('timestamp with time zone', 'timestamp without time zone'):
        return datetime.datetime.fromisoformat(value)
    if db_type == 'date':
        return datetime.date.fromisoformat(value)
    return value


def run(payload):
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'app.settings')
    import django
    django.setup()
    from django.apps import apps
    from django.core.management.color import no_style
    from django.db import connection, transaction
    expected_schema = 'portal_' + ('core' if payload['service'] == 'portal' else payload['service'])
    models = {m._meta.db_table: m for m in apps.get_models(include_auto_created=True)
              if m._meta.managed and not m._meta.proxy}
    tables = payload['tables']
    if not tables or set(tables) - set(models):
        raise ValueError('Source tables do not match service models')
    for name, table in tables.items():
        fields = {field.column: field for field in models[name]._meta.local_fields}
        columns = {column['name'] for column in table['columns']}
        if columns != set(fields) or any(set(row) != columns for row in table['rows']):
            raise ValueError('Source columns differ from service model')
        if digest(table['rows']) != table['sha256']:
            raise ValueError('Source table checksum mismatch')
    with connection.cursor() as cursor:
        cursor.execute('SELECT current_database(), current_user, current_schema()')
        if cursor.fetchone() != ('updspace', expected_schema, expected_schema):
            raise ValueError('Wrong database/role/schema; refusing replacement')
        known = set(connection.introspection.table_names(cursor))
        if known != set(models) | {'django_migrations'}:
            raise ValueError('Unexpected destination tables; refusing replacement')
    if payload.get('replace_trial') is not True:
        raise ValueError('Explicit trial replacement is required')
    quote = connection.ops.quote_name
    proof = []
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute('TRUNCATE ' + ', '.join(quote(name) for name in sorted(models)) + ' RESTART IDENTITY')
            cursor.execute('SET CONSTRAINTS ALL DEFERRED')
            for name, table in tables.items():
                fields = {field.column: field for field in models[name]._meta.local_fields}
                columns = [column['name'] for column in table['columns']]
                query = 'INSERT INTO ' + quote(name) + ' (' + ','.join(quote(col) for col in columns) + ') VALUES (' + ','.join(['%s'] * len(columns)) + ')'
                values = [tuple(prepare_value(row[col], fields[col], connection) for col in columns) for row in table['rows']]
                if values:
                    cursor.executemany(query, values)
            for statement in connection.ops.sequence_reset_sql(no_style(), list(models.values())):
                cursor.execute(statement)
            connection.check_constraints()
            for name, table in tables.items():
                columns = table['columns']
                cursor.execute('SELECT ' + ','.join(quote(col['name']) for col in columns) + ' FROM ' + quote(name))
                rows = [{col['name']: normalize(value, col['type']) for col, value in zip(columns, row)} for row in cursor.fetchall()]
                fingerprint = digest(rows)
                if len(rows) != len(table['rows']) or fingerprint != table['sha256']:
                    raise ValueError('Destination verification failed; transaction rolled back')
                proof.append({'table': name, 'rows': len(rows), 'sha256': fingerprint})
    return {'service': payload['service'], 'schema': expected_schema, 'tables': proof, 'verified': True}


if __name__ == '__main__':
    try:
        print(json.dumps(run(json.load(sys.stdin))))
    except Exception as error:
        print(json.dumps({'error': type(error).__name__, 'message': 'Import stopped; rows/credentials suppressed'}))
        raise SystemExit(1) from None
