"""Preserve an explicitly installed Storm pin across legacy Secret imports."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('sync_secrets', Path(__file__).with_name('sync-secrets.py'))
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class StormPinTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'storm-pin.json'
        self.namespace = {}
        exec(module.PIN_OVERRIDE, self.namespace)
        self.apply = self.namespace['apply_storm_pin']
        self.options = self.namespace['STORM_AGENT'] + ' -DstormType=local -Dstorm.core.updateUrl= -XX:+UseZGC'
        self.values = {'JAVA_TOOL_OPTIONS': '-javaagent:/workshop/storm-bootstrap.jar',
                       'RCON_PASSWORD': 'fixture-not-a-secret', 'PZ_SERVER_NAME': 'fixture'}

    def tearDown(self):
        self.tmp.cleanup()

    def write_pin(self, obj=None, mode=0o600):
        self.path.write_text(json.dumps(obj if obj is not None else {'JAVA_TOOL_OPTIONS': self.options}))
        self.path.chmod(mode)

    def test_valid_pin_changes_only_jvm_options(self):
        self.write_pin()
        with patch.dict(self.namespace, require_storm_trusted_path=lambda path: None):
            actual = self.apply(self.values, self.path)
        self.assertEqual(actual, {**self.values, 'JAVA_TOOL_OPTIONS': self.options})
        self.assertNotEqual(self.values['JAVA_TOOL_OPTIONS'], self.options)

    def test_missing_pin_cannot_overwrite_an_existing_local_secret(self):
        self.assertEqual(self.apply(self.values, self.path), self.values)
        with self.assertRaisesRegex(RuntimeError, 'requires its trusted override'):
            self.apply(self.values, self.path, current_options=self.options)

    def test_symlink_private_mode_and_oversized_files_are_refused(self):
        self.write_pin(mode=0o644)
        with patch.dict(self.namespace, require_storm_trusted_path=lambda path: None):
            with self.assertRaisesRegex(RuntimeError, 'private bounded'):
                self.apply(self.values, self.path)
        self.path.chmod(0o600)
        self.path.write_text('x' * 16385)
        with patch.dict(self.namespace, require_storm_trusted_path=lambda path: None):
            with self.assertRaisesRegex(RuntimeError, 'private bounded'):
                self.apply(self.values, self.path)
        self.path.unlink()
        self.path.symlink_to(self.path.parent / 'missing')
        with self.assertRaises(RuntimeError):
            self.apply(self.values, self.path)

    def test_extra_secret_keys_remote_mode_and_auto_update_are_refused(self):
        invalid = [
            {'JAVA_TOOL_OPTIONS': self.options, 'RCON_PASSWORD': 'other'},
            {'JAVA_TOOL_OPTIONS': self.options.replace('-DstormType=local', '-DstormType=workshop')},
            {'JAVA_TOOL_OPTIONS': self.options + ' -Dstorm.core.updateUrl=https://example.invalid'},
            {'JAVA_TOOL_OPTIONS': self.options + '\nPZ_SERVER_NAME=other'},
        ]
        for value in invalid:
            with self.subTest(value=value):
                self.write_pin(value)
                with patch.dict(self.namespace, require_storm_trusted_path=lambda path: None):
                    with self.assertRaises(RuntimeError):
                        self.apply(self.values, self.path)

    def test_untrusted_parent_is_rejected_without_reading_override(self):
        self.write_pin()
        # /tmp is writable by non-root users, so this deliberately cannot pass
        # the real root-owned /etc path gate, even if the file itself is 0600.
        with self.assertRaisesRegex(RuntimeError, 'controlled by root'):
            self.apply(self.values, self.path)
