import unittest

from build import build


class LocalMonitoringTests(unittest.TestCase):
    def test_no_nodeport_bypasses_identity_gate(self):
        resources, _ = build()
        public = [r for r in resources if r['kind'] == 'Service' and r['spec']['type'] != 'ClusterIP']
        self.assertEqual(public, [])
        policy = next(r for r in resources if r['metadata']['name'] == 'grafana-lan')
        self.assertEqual(policy['spec']['ingress'], [])

    def test_collector_routes_to_local_backends_without_cloud_secrets(self):
        _, collector = build()
        self.assertEqual(set(collector['exporters']), {'prometheus_remote_write/local', 'otlp_http/local_logs'})
        for name, pipeline in collector['service']['pipelines'].items():
            expected = 'otlp_http/local_logs' if name.startswith('logs') else 'prometheus_remote_write/local'
            self.assertEqual(pipeline['exporters'], [expected])
        self.assertIn('file_storage', collector['extensions'])


if __name__ == '__main__':
    unittest.main()
