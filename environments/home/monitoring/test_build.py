import unittest

from build import build


class LocalMonitoringTests(unittest.TestCase):
    def test_no_nodeport_bypasses_identity_gate(self):
        resources, _ = build()
        public = [r for r in resources if r['kind'] == 'Service' and r['spec']['type'] != 'ClusterIP']
        self.assertEqual(public, [])
        policy = next(r for r in resources if r['metadata']['name'] == 'grafana-lan')
        self.assertEqual(policy['spec']['ingress'], [])

    def test_native_oauth_uses_subject_pkce_and_strict_role(self):
        resources, _ = build()
        grafana = next(x for x in resources if x['kind']=='Deployment' and x['metadata']['name']=='grafana')
        env = {x['name']:x for x in grafana['spec']['template']['spec']['containers'][0]['env']}
        for key in ('AUTO_LOGIN','ALLOW_SIGN_UP','USE_PKCE','USE_REFRESH_TOKEN','ROLE_ATTRIBUTE_STRICT'):
            self.assertEqual(env['GF_AUTH_GENERIC_OAUTH_'+key]['value'],'true')
        self.assertEqual(env['GF_AUTH_GENERIC_OAUTH_LOGIN_ATTRIBUTE_PATH']['value'],'preferred_username')
        self.assertEqual(env['GF_AUTH_GENERIC_OAUTH_ALLOW_ASSIGN_GRAFANA_ADMIN']['value'],'false')
        self.assertEqual(env['GF_AUTH_OAUTH_ALLOW_INSECURE_EMAIL_LOOKUP']['value'],'false')
        self.assertEqual(env['GF_AUTH_GENERIC_OAUTH_CLIENT_SECRET']['valueFrom']['secretKeyRef']['name'],'grafana-oidc')
        self.assertNotIn('GF_AUTH_PROXY_ENABLED', env)

    def test_collector_routes_to_local_backends_without_cloud_secrets(self):
        _, collector = build()
        self.assertEqual(set(collector['exporters']), {'prometheus_remote_write/local', 'otlp_http/local_logs'})
        for name, pipeline in collector['service']['pipelines'].items():
            expected = 'otlp_http/local_logs' if name.startswith('logs') else 'prometheus_remote_write/local'
            self.assertEqual(pipeline['exporters'], [expected])
        self.assertIn('file_storage', collector['extensions'])


if __name__ == '__main__':
    unittest.main()
