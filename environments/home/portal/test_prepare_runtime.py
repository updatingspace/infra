import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('prepare_runtime', Path(__file__).with_name('prepare-runtime.py'))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class RuntimeTests(unittest.TestCase):
    def test_replaces_cloud_backends_without_rotating_identity_keys(self):
        original = {'DJANGO_SECRET_KEY': 'synthetic-django', 'BFF_INTERNAL_HMAC_SECRET': 'synthetic-hmac',
                    'BFF_OIDC_CLIENT_SECRET': 'synthetic-client', 'BFF_UPSTREAM_ACCESS_INVOKE_URL': 'cloud',
                    'YDB_DATABASE': 'cloud', 'YMQ_OUTBOX_QUEUE': 'cloud', 'YC_IAM_TOKEN': 'cloud'}
        storage = {'endpoint': 'https://storage.updspace.com', 'region': 'garage',
                   'access_key_id': 'synthetic-key', 'secret_access_key': 'synthetic-storage'}
        env = module.environment('bff', original, {'portal_bff': 'a/b:c@d'}, storage)
        self.assertFalse(any(key.startswith(('YDB_', 'YMQ_', 'YC_')) or key.endswith('_INVOKE_URL') for key in env))
        self.assertEqual(env['BFF_OIDC_CLIENT_SECRET'], original['BFF_OIDC_CLIENT_SECRET'])
        self.assertEqual(env['BFF_INTERNAL_HMAC_SECRET'], original['BFF_INTERNAL_HMAC_SECRET'])
        self.assertEqual(env['ACCESS_BASE_URL'], 'http://access:8000/api/v1')
        self.assertIn('a%2Fb%3Ac%40d@postgres.', env['DATABASE_URL'])
        self.assertEqual(env['ID_BASE_URL'], 'https://id.updspace.com/api/v1')
        self.assertEqual(env['S3_FORCE_PATH_STYLE'], '1')

    def test_stage_keeps_public_issuer_and_only_changes_backend(self):
        storage = {'endpoint': 'https://storage.updspace.com', 'region': 'garage',
                   'access_key_id': 'synthetic-key', 'secret_access_key': 'synthetic-storage'}
        env = module.environment('bff', {}, {'portal_bff': 'synthetic-password'}, storage, id_stage=True)
        self.assertEqual(env['ID_PUBLIC_BASE_URL'], 'https://id.updspace.com')
        self.assertEqual(env['ID_BASE_URL'], 'http://id.updspace-id.svc.cluster.local:8089/api/v1')

    def test_refuses_unexpected_service_set(self):
        with self.assertRaises(ValueError):
            module.objects({'services': {}}, {}, {}, {})


if __name__ == '__main__':
    unittest.main()
