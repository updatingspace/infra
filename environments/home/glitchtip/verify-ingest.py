#!/usr/bin/env python3
"""Send labelled synthetic error/span envelopes and verify their stored records."""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile
import time
from urllib.parse import urlsplit
import uuid


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--origin-ip')
    args = parser.parse_args()
    private = json.loads(Path('/opt/updspace-infra/private/glitchtip-credentials.json').read_text())
    dsn = urlsplit(private['dsn'])
    host = dsn.hostname
    project = int(dsn.path.strip('/'))
    error_id, transaction_id, trace_id = [uuid.uuid4().hex for _ in range(3)]
    now = time.time()
    name = 'infra-acceptance-' + transaction_id[:12]
    error = {'event_id': error_id, 'timestamp': now, 'platform': 'python', 'level': 'error',
             'logger': 'infra.acceptance', 'message': 'Synthetic installation acceptance',
             'exception': {'values': [{'type': 'InfrastructureAcceptance',
                 'value': 'Synthetic check; no production incident'}]}, 'tags': {'acceptance': name}}
    transaction = {'type': 'transaction', 'event_id': transaction_id, 'platform': 'python',
        'transaction': name, 'start_timestamp': now - 0.02, 'timestamp': now,
        'contexts': {'trace': {'trace_id': trace_id, 'span_id': '1234567890abcdef', 'op': 'http.server', 'status': 'ok'}},
        'spans': [{'trace_id': trace_id, 'span_id': 'abcdef1234567890', 'parent_span_id': '1234567890abcdef',
            'op': 'db.query', 'description': 'SELECT 1 (synthetic)', 'start_timestamp': now - 0.015, 'timestamp': now - 0.005}],
        'tags': {'acceptance': name}}
    extra = [] if not args.origin_ip else ['--resolve', host + ':443:' + args.origin_ip]
    for kind, event in [('event', error), ('transaction', transaction)]:
        body = json.dumps(event).encode()
        envelope = json.dumps({'event_id': event['event_id']}).encode() + b'\n' + \
            json.dumps({'type': kind, 'length': len(body)}).encode() + b'\n' + body + b'\n'
        with tempfile.NamedTemporaryFile() as file:
            file.write(envelope)
            file.flush()
            config = 'header = "X-Sentry-Auth: Sentry sentry_version=7,sentry_key=' + dsn.username + '"\n'
            result = subprocess.run(['curl', '-sS', '--max-time', '20', '--config', '-', '--data-binary', '@' + file.name,
                '-H', 'Content-Type: application/x-sentry-envelope', '-w', '\n%{http_code}',
                'https://' + host + '/api/' + str(project) + '/envelope/'] + extra,
                input=config.encode(), capture_output=True)
        assert result.returncode == 0, ('ingest transfer failed', kind, result.returncode)
        _, code = result.stdout.rsplit(b'\n', 1)
        assert code == b'200', ('ingest rejected', kind, code)
    query = """import json
from apps.issue_events.models import IssueEvent
from apps.performance.models import TransactionGroup,SpanStaging
print(json.dumps({'error':IssueEvent.objects.filter(event_id='%s',issue__project_id=%d).exists(),
 'transaction':TransactionGroup.objects.filter(transaction='%s',project_id=%d).exists(),
 'span':SpanStaging.objects.filter(transaction_name='%s',project_id=%d,op='db.query').exists()}))
""" % (error_id, project, name, project, name, project)
    deadline = time.monotonic() + 30
    while True:
        result = subprocess.run(['k3s', 'kubectl', '-n', 'glitchtip', 'exec', 'deployment/glitchtip', '--',
            'python', 'manage.py', 'shell', '-c', query], capture_output=True, text=True, check=True)
        evidence = json.loads(result.stdout.strip().splitlines()[-1])
        if all(evidence.values()) or time.monotonic() >= deadline:
            break
        time.sleep(2)
    print(json.dumps({'test': name, 'originOverride': args.origin_ip, 'http': 200, **evidence}))
    assert all(evidence.values()), 'Envelope accepted but not fully processed'


if __name__ == '__main__':
    main()
