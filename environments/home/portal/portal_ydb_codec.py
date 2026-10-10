"""Lossless normalization and fingerprints for the observed Portal YDB types."""
import datetime
import hashlib
import json
import uuid


def normalize(value, type_name):
    if value is None:
        return None
    kind = type_name.removesuffix('?')
    if kind == 'UUID':
        return str(uuid.UUID(str(value)))
    if kind in ('Datetime', 'Timestamp'):
        if isinstance(value, int):
            value = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc) + datetime.timedelta(microseconds=value if kind == 'Timestamp' else value * 1_000_000)
        if not isinstance(value, datetime.datetime):
            raise TypeError('Expected datetime or epoch value')
        return value.replace(tzinfo=value.tzinfo or datetime.timezone.utc).astimezone(datetime.timezone.utc).isoformat()
    if kind == 'Date':
        if isinstance(value, int):
            value = datetime.date(1970, 1, 1) + datetime.timedelta(days=value)
        return value.isoformat()
    if kind in ('Json', 'JsonDocument'):
        return json.loads(value) if isinstance(value, str) else value
    if kind in ('Utf8', 'Bool', 'Int8', 'Int16', 'Int32', 'Int64', 'Uint8', 'Uint16', 'Uint32', 'Uint64', 'Float', 'Double'):
        return value
    raise TypeError('Unsupported source type: ' + type_name)


def digest(rows):
    values = sorted(json.dumps(row, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False) for row in rows)
    return hashlib.sha256(('\n'.join(values) + '\n').encode()).hexdigest()
