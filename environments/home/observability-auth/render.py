#!/usr/bin/env python3
"""Render the bounded browser-session service; ID owns authorization policy."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
IMAGE = 'quay.io/oauth2-proxy/oauth2-proxy:v7.14.0@sha256:47b1db28264a646c5c07e1fb690e3deef7edbbb487030ad7c1f0b22f942d6837'
NS = 'observability-auth'
LABEL = {'app.kubernetes.io/name': 'oauth2-proxy'}


def peer(namespace, name):
    return {'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': namespace}},
            'podSelector': {'matchLabels': {'app.kubernetes.io/name': name}}}


def render():
    config = (ROOT / 'oauth2-proxy.cfg').read_text()
    def resource(kind, name, spec=None, namespace=NS, **fields):
        version = {'Deployment': 'apps/v1', 'NetworkPolicy': 'networking.k8s.io/v1'}.get(kind, 'v1')
        obj = {'apiVersion': version, 'kind': kind, 'metadata': {'name': name}}
        if namespace:
            obj['metadata']['namespace'] = namespace
        if spec is not None:
            obj['spec'] = spec
        return obj | fields
    edge = peer('edge', 'caddy')
    identity = peer('updspace-id', 'id')
    proxy = peer(NS, 'oauth2-proxy')
    tcp = lambda port: {'protocol': 'TCP', 'port': port}
    return [
        resource('Namespace', NS, namespace=None),
        resource('ResourceQuota', 'budget', {'hard': {'pods': '2', 'requests.cpu': '100m',
            'requests.memory': '128Mi', 'limits.cpu': '500m', 'limits.memory': '256Mi'}}),
        resource('ConfigMap', 'oauth2-proxy', data={'oauth2-proxy.cfg': config}),
        resource('Service', 'oauth2-proxy', {'type': 'ClusterIP', 'selector': LABEL,
            'ports': [{'name': 'http', 'port': 4180, 'targetPort': 'http'}]}),
        resource('Deployment', 'oauth2-proxy', {
            'replicas': 1, 'strategy': {'type': 'Recreate'}, 'selector': {'matchLabels': LABEL},
            'template': {'metadata': {'labels': LABEL, 'annotations': {'checksum/config': hashlib.sha256(config.encode()).hexdigest()}},
                'spec': {'automountServiceAccountToken': False,
                    'securityContext': {'runAsNonRoot': True, 'runAsUser': 65532, 'runAsGroup': 65532,
                        'seccompProfile': {'type': 'RuntimeDefault'}},
                    'containers': [{'name': 'oauth2-proxy', 'image': IMAGE,
                        'args': ['--config=/etc/oauth2-proxy/oauth2-proxy.cfg'],
                        'envFrom': [{'secretRef': {'name': 'oauth2-proxy'}}],
                        'ports': [{'name': 'http', 'containerPort': 4180}],
                        'resources': {'requests': {'cpu': '50m', 'memory': '64Mi'},
                            'limits': {'cpu': '250m', 'memory': '128Mi'}},
                        'securityContext': {'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True,
                            'capabilities': {'drop': ['ALL']}},
                        'volumeMounts': [{'name': 'config', 'mountPath': '/etc/oauth2-proxy', 'readOnly': True}],
                        'readinessProbe': {'httpGet': {'path': '/ready', 'port': 'http'}, 'timeoutSeconds': 3},
                        'livenessProbe': {'httpGet': {'path': '/ping', 'port': 'http'}, 'timeoutSeconds': 3}}],
                    'volumes': [{'name': 'config', 'configMap': {'name': 'oauth2-proxy'}}]}}}),
        resource('NetworkPolicy', 'oauth2-proxy', {'podSelector': {'matchLabels': LABEL},
            'policyTypes': ['Ingress', 'Egress'],
            'ingress': [{'from': [edge], 'ports': [tcp(4180)]}],
            'egress': [{'to': [identity], 'ports': [tcp(8089)]},
                {'to': [{'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'kube-system'}},
                    'podSelector': {'matchLabels': {'k8s-app': 'kube-dns'}}}],
                    'ports': [tcp(53), {'protocol': 'UDP', 'port': 53}]}]}),
        resource('NetworkPolicy', 'caddy-to-observability-auth', {'podSelector': {'matchLabels': edge['podSelector']['matchLabels']},
            'policyTypes': ['Egress'], 'egress': [{'to': [proxy], 'ports': [tcp(4180)]}]}, namespace='edge'),
        resource('NetworkPolicy', 'id-from-observability-auth', {'podSelector': identity['podSelector'],
            'policyTypes': ['Ingress'], 'ingress': [{'from': [proxy], 'ports': [tcp(8089)]}]}, namespace='updspace-id'),
    ]


if __name__ == '__main__':
    print(json.dumps({'apiVersion': 'v1', 'kind': 'List', 'items': render()}, indent=2))
