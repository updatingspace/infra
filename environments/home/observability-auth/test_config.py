import importlib.util
import json
from pathlib import Path
import tomllib
import unittest
from render import render

ROOT = Path(__file__).resolve().parent


class ConfigurationTests(unittest.TestCase):
    def test_host_only_cookie_and_exact_callbacks(self):
        cfg = tomllib.loads((ROOT/'oauth2-proxy.cfg').read_text())
        client = json.loads((ROOT/'client.json').read_text())
        self.assertFalse(client['is_public'])
        self.assertEqual({f'https://{h}/oauth2/callback' for h in cfg['whitelist_domains']}, set(client['redirect_uris']))
        self.assertEqual(cfg['client_id'], client['client_id'])
        self.assertEqual(set(cfg['scope'].split()), set(client['allowed_scopes']))
        self.assertNotIn('cookie_domains', cfg)
        self.assertTrue(cfg['cookie_name'].startswith('__Host-'))
        for key in ('cookie_secure', 'cookie_httponly', 'set_xauthrequest', 'pass_access_token'):
            self.assertTrue(cfg[key])
        for key in ('skip_jwt_bearer_tokens', 'insecure_oidc_skip_nonce', 'pass_basic_auth', 'request_logging'):
            self.assertFalse(cfg[key])
        self.assertEqual(cfg['code_challenge_method'], 'S256')
        self.assertEqual(cfg['prompt'], 'consent')
        self.assertNotIn('insecure_oidc_skip_issuer_verification', cfg)
        self.assertNotIn('ssl_insecure_skip_verify', cfg)
        self.assertNotIn('trusted_ips', cfg)

    def test_open_streams_reauthorize_within_one_minute(self):
        caddy = (ROOT.parent/'edge/Caddyfile').read_text()
        self.assertEqual(caddy.count('stream_timeout 1m'), 5)

    def test_private_service_is_bounded_and_accepts_only_edge(self):
        objects = render()
        self.assertFalse(any(x['kind'] == 'Secret' for x in objects))
        service = next(x for x in objects if x['kind'] == 'Service')
        self.assertEqual(service['spec']['type'], 'ClusterIP')
        deployment = next(x for x in objects if x['kind'] == 'Deployment')
        container = deployment['spec']['template']['spec']['containers'][0]
        self.assertIn('@sha256:', container['image'])
        self.assertEqual(container['resources']['limits']['memory'], '128Mi')
        policy = next(x for x in objects if x['kind'] == 'NetworkPolicy' and x['metadata']['namespace'] == 'observability-auth')
        self.assertEqual(policy['spec']['ingress'][0]['from'][0]['podSelector']['matchLabels']['app.kubernetes.io/name'], 'caddy')

    def test_client_bootstrap_refuses_unrelated_credentials(self):
        spec = importlib.util.spec_from_file_location('bootstrap', ROOT/'provision-client.py')
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        cfg = json.loads((ROOT/'client.json').read_text())
        credentials = {'id': 42, 'client_secret_hash': 'fixture-hash'}
        row = dict(cfg, id=42, client_secret_hash='fixture-hash')
        for key in ('redirect_uris','allowed_scopes','grant_types'):
            row[key] = json.dumps(row[key])
        self.assertTrue(module.matches(row, cfg, credentials))
        for key, value in [('id', 43), ('client_secret_hash', 'changed'), ('is_public', True),
                           ('redirect_uris', '["https://evil.invalid/callback"]')]:
            self.assertFalse(module.matches(row | {key:value}, cfg, credentials))
        sql = module.create_sql(cfg, credentials)
        self.assertIn('Ensure(', sql)
        self.assertIn('INSERT INTO idp_oidcclient', sql)
        self.assertNotIn('UPDATE ', sql)
        self.assertNotIn('auth_user', sql)
        self.assertEqual(module.literal('quote"\nline'), '"quote\\"\\nline"')

    def test_status_includes_the_whole_portal_without_private_urls(self):
        cfg = json.loads((ROOT.parent/'uptime-kuma/config.json').read_text())
        names = {m['name'] for m in cfg['monitors']}
        self.assertTrue({'UpdSpace ID', 'UpdSpace Portal', 'Portal API', 'GlitchTip'} <= names)
        expected = {'Portal '+name for name in ('access','activity','events','featureflags','gamification','portal','voting')}
        self.assertTrue(expected <= names)
        for group in cfg['publicGroups']:
            for monitor in group['monitors']:
                if monitor['name'].startswith(('Portal ', 'UpdSpace ')):
                    self.assertFalse(monitor['sendUrl'])


if __name__ == '__main__': unittest.main()
