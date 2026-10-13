import importlib.util
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('verify_access', Path(__file__).with_name('verify-access.py'))
access = importlib.util.module_from_spec(spec)
spec.loader.exec_module(access)


class AccessTests(unittest.TestCase):
    def test_extract_assets_without_forwarding_credentials_to_external_hosts(self):
        html = b'''<script src="public/build/app.js"></script><script src="/public/build/app.js"></script>
        <script src="//external.example/tracker.js"></script><script src="http://grafana.updspace.com/x.js"></script>
        <link rel="stylesheet" href="/public/build/app.css"><link rel="icon" href="/favicon.ico">'''
        self.assertEqual(access.grafana_assets(html), {
            access.GRAFANA + '/public/build/app.js': 'script', access.GRAFANA + '/public/build/app.css': 'style'})

    def test_error_html_is_not_a_successful_grafana_page(self):
        with self.assertRaises(AssertionError):
            access.grafana_assets(b'<html>upstream error</html>')

    def test_http_200_with_truncated_body_fails(self):
        result = subprocess.CompletedProcess([], 28, b'partial\n200\napplication/javascript\n', b'timed out')
        with patch.object(access.subprocess, 'run', return_value=result) as run:
            with self.assertRaisesRegex(AssertionError, 'incomplete download'):
                access.get(access.GRAFANA + '/app.js', headers=('Authorization: Bearer test-secret',))
            self.assertNotIn('test-secret', ' '.join(run.call_args.args[0]))

    def test_legacy_oauth_parameter_is_rejected_before_following_redirect(self):
        target = 'https://id.updspace.com/oauth/authorize?client_id=observability&redirect_uri=https%3A%2F%2Fgrafana.updspace.com%2Foauth2%2Fcallback&code_challenge_method=S256&nonce=fixture&approval_prompt=force'
        with patch.object(access, 'get', return_value=(302, b'', '', target)):
            with self.assertRaisesRegex(AssertionError, 'legacy approval_prompt'):
                access.verify_login_start('grafana.updspace.com')

    def test_asset_requires_real_complete_asset_content(self):
        for response in [(200, b'<html>error</html>', 'application/javascript'),
                         (200, b'login', 'text/html'), (401, b'no', 'text/javascript')]:
            with self.subTest(response=response), patch.object(access, 'get', return_value=(*response, '')):
                with self.assertRaises(AssertionError):
                    access.verify_asset(access.GRAFANA + '/app.js', 'script', None, None)
        with patch.object(access, 'get', return_value=(200, b'alert(1)', 'text/javascript', '')):
            self.assertEqual(access.verify_asset(access.GRAFANA + '/app.js', 'script', None, None)['bytes'], 8)


if __name__ == '__main__':
    unittest.main()
