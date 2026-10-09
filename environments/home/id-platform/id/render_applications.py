#!/usr/bin/env python3
"""Render the staged ID workload and its network boundaries."""
import json
from pathlib import Path
import yaml

root = Path(__file__).resolve().parent
ns = 'updspace-id'
labels = {'app.kubernetes.io/name': 'id', 'app.kubernetes.io/part-of': ns}
runtime = 'docker.io/updspace/id-runtime@sha256:a1ff985aa5ae67249daee1e98142e323d7f5fbaee95c67ad82472444d3ee475f'
web = 'docker.io/updspace/id-web@sha256:08db40c0579fc76019124918b504dd7352956c0599fc18bb94d77f2c52e8bd12'
caddy = 'docker.io/local/caddy@sha256:5f5c8640aae01df9654968d946d8f1a56c497f1dd5c5cda4cf95ab7c14d58648'
security = {'runAsNonRoot': True, 'allowPrivilegeEscalation': False,
            'readOnlyRootFilesystem': True, 'capabilities': {'drop': ['ALL']}}

def resource(api, kind, name, spec=None, namespace=ns, **fields):
    obj = {'apiVersion': api, 'kind': kind, 'metadata': {'name': name}}
    if namespace:
        obj['metadata']['namespace'] = namespace
    if spec is not None:
        obj['spec'] = spec
    obj.update(fields)
    return obj

def peer(namespace, match):
    return {'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': namespace}},
            'podSelector': {'matchLabels': match}}

def port(number, protocol='TCP'):
    return {'protocol': protocol, 'port': number}

dns = {'to': [peer('kube-system', {'k8s-app': 'kube-dns'})], 'ports': [port(53), port(53, 'UDP')]}
docs = [resource('v1', 'Namespace', ns, namespace=None),
        resource('v1', 'ConfigMap', 'id-router', data={'Caddyfile': (root/'Caddyfile.internal').read_text()})]
containers = []
for name, number in [('api', 8081), ('sessions', 8082), ('mutations', 8083), ('web', 3000), ('jobs', 8084)]:
    api = name in ('api', 'sessions', 'mutations')
    probe = {'httpGet': {'path': '/readyz', 'port': number}} if api else {'tcpSocket': {'port': number}}
    container = {
        'name': name, 'image': web if name == 'web' else runtime, 'imagePullPolicy': 'Never',
        'envFrom': [{'secretRef': {'name': 'id-' + name}}],
        'env': [{'name': 'PORT', 'value': str(number)}, {'name': 'HOST', 'value': '0.0.0.0'}],
        'ports': [{'name': name, 'containerPort': number}], 'securityContext': security,
        'resources': {'requests': {'cpu': '50m', 'memory': '128Mi'},
                      'limits': {'cpu': '1', 'memory': '1Gi' if api else '512Mi'}},
        'startupProbe': {**probe, 'failureThreshold': 60, 'periodSeconds': 5, 'timeoutSeconds': 3},
        'readinessProbe': {**probe, 'periodSeconds': 10, 'timeoutSeconds': 3},
        'volumeMounts': [{'name': 'ydb-ca', 'mountPath': '/etc/ydb', 'readOnly': True}],
    }
    if name != 'web':
        container['command'] = ['/usr/local/bin/id-api' if api else '/usr/local/bin/id-jobs']
    if name == 'jobs':
        container['args'] = ['--serve']
    containers.append(container)
containers.append({
    'name': 'router', 'image': caddy, 'imagePullPolicy': 'Never',
    'command': ['caddy', 'run', '--config', '/etc/id-router/Caddyfile', '--adapter', 'caddyfile'],
    'ports': [{'name': 'http', 'containerPort': 8089}], 'securityContext': {**security, 'capabilities': {'drop': ['ALL'], 'add': ['NET_BIND_SERVICE']}},
    'resources': {'requests': {'cpu': '10m', 'memory': '32Mi'}, 'limits': {'cpu': '500m', 'memory': '128Mi'}},
    'env': [{'name': 'XDG_CONFIG_HOME', 'value': '/tmp/config'}, {'name': 'XDG_DATA_HOME', 'value': '/tmp/data'}],
    'volumeMounts': [{'name': 'router', 'mountPath': '/etc/id-router', 'readOnly': True},
                     {'name': 'router-tmp', 'mountPath': '/tmp'}],
    'readinessProbe': {'tcpSocket': {'port': 8089}},
})
docs.append(resource('apps/v1', 'Deployment', 'id', {
    'replicas': 0, 'strategy': {'type': 'Recreate'}, 'selector': {'matchLabels': labels},
    'template': {'metadata': {'labels': labels}, 'spec': {
        'automountServiceAccountToken': False, 'terminationGracePeriodSeconds': 60,
        'nodeSelector': {'kubernetes.io/hostname': 'updspace-home'},
        'securityContext': {'runAsUser': 65532, 'runAsGroup': 65532, 'seccompProfile': {'type': 'RuntimeDefault'}},
        'containers': containers,
        'volumes': [{'name': 'ydb-ca', 'configMap': {'name': 'id-ydb-ca'}},
                    {'name': 'router', 'configMap': {'name': 'id-router'}}, {'name': 'router-tmp', 'emptyDir': {}}],
    }},
}))
for name, number in [('id', 8089), ('id-jobs', 8084)]:
    docs.append(resource('v1', 'Service', name, {'selector': labels, 'ports': [{'name': 'http', 'port': number, 'targetPort': number}]}))
docs.append(resource('networking.k8s.io/v1', 'NetworkPolicy', 'default-deny', {
    'podSelector': {}, 'policyTypes': ['Ingress', 'Egress'], 'ingress': [], 'egress': []}))
edge = peer('edge', {'app.kubernetes.io/name': 'caddy'})
timer_labels = {'app.kubernetes.io/name': 'id-timer'}
docs.append(resource('networking.k8s.io/v1', 'NetworkPolicy', 'id-runtime', {
    'podSelector': {'matchLabels': labels}, 'policyTypes': ['Ingress', 'Egress'],
    'ingress': [{'from': [edge], 'ports': [port(8089)]},
                {'from': [peer(ns, timer_labels)], 'ports': [port(8084)]}],
    'egress': [dns, {'to': [peer('updspace-data', {'app.kubernetes.io/name': 'id-ydb'})], 'ports': [port(2135)]},
               {'to': [{'ipBlock': {'cidr': '0.0.0.0/0', 'except': ['10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '169.254.0.0/16', '127.0.0.0/8']}}],
                'ports': [port(443), port(587)]}],
}))
docs.append(resource('networking.k8s.io/v1', 'NetworkPolicy', 'id-ydb-clients', {
    'podSelector': {'matchLabels': {'app.kubernetes.io/name': 'id-ydb'}}, 'policyTypes': ['Ingress'],
    'ingress': [{'from': [peer(ns, labels)], 'ports': [port(2135)]}],
}, namespace='updspace-data'))
docs.append(resource('networking.k8s.io/v1', 'NetworkPolicy', 'caddy-to-id', {
    'podSelector': {'matchLabels': {'app.kubernetes.io/name': 'caddy'}}, 'policyTypes': ['Egress'],
    'egress': [{'to': [peer(ns, labels)], 'ports': [port(8089)]}],
}, namespace='edge'))
docs.append(resource('networking.k8s.io/v1', 'NetworkPolicy', 'id-timers', {
    'podSelector': {'matchLabels': timer_labels}, 'policyTypes': ['Egress'],
    'egress': [dns, {'to': [peer(ns, labels)], 'ports': [port(8084)]}],
}))
event = json.dumps({'messages': [{'event_metadata': {'event_type': 'yandex.cloud.events.serverless.triggers.TimerMessage'}, 'details': {}}]}, separators=(',', ':'))
for name, schedule, path in [
    ('recover-mail', '* * * * *', '/internal/jobs/recover-mail'),
    ('recover-security-mail', '*/5 * * * *', '/internal/jobs/recover-security-mail'),
    ('recover-verify-mail', '*/5 * * * *', '/internal/jobs/recover-verify-mail'),
    ('recover-export', '*/5 * * * *', '/internal/jobs/recover-export'),
    ('refresh-gravatars', '17 * * * *', '/refresh-gravatars'),
]:
    pod = {
        'automountServiceAccountToken': False, 'restartPolicy': 'Never',
        'securityContext': {'runAsUser': 65532, 'runAsGroup': 65532, 'seccompProfile': {'type': 'RuntimeDefault'}},
        'containers': [{
            'name': 'invoke', 'image': caddy, 'imagePullPolicy': 'Never',
            'command': ['wget', '-q', '-O', '-', '-T', '550', '--header=Content-Type: application/json', '--post-data=' + event, 'http://id-jobs:8084' + path],
            'securityContext': security,
            'resources': {'requests': {'cpu': '10m', 'memory': '16Mi'}, 'limits': {'cpu': '100m', 'memory': '32Mi'}},
        }],
    }
    docs.append(resource('batch/v1', 'CronJob', name, {
        'suspend': True, 'schedule': schedule, 'timeZone': 'Etc/UTC', 'concurrencyPolicy': 'Forbid',
        'startingDeadlineSeconds': 120, 'successfulJobsHistoryLimit': 1, 'failedJobsHistoryLimit': 3,
        'jobTemplate': {'spec': {'activeDeadlineSeconds': 600, 'backoffLimit': 2,
                                 'template': {'metadata': {'labels': timer_labels}, 'spec': pod}}},
    }))
assert len(containers) == 6 and sum(d['kind'] == 'CronJob' for d in docs) == 5
assert all(d['spec']['suspend'] for d in docs if d['kind'] == 'CronJob')
output = yaml.safe_dump_all(docs, sort_keys=False)
assert list(yaml.safe_load_all(output)) == docs
(root/'applications.yaml').write_text('# Staged deployment: scale only after secrets and restored YDB are verified.\n' + output)
print(f'{len(docs)} resources written')
