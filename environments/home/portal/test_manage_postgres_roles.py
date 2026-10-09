import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location(
    "role_manager", Path(__file__).with_name("manage-postgres-roles.py")
)
manager = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manager)
CONFIG = {"database": "updspace", "roles": ["portal_bff", "portal_core"]}
CLEAN = {"existing_roles": CONFIG["roles"], "issues": [], "blockers": []}


class RoleManagerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config = self.root / "roles.json"
        self.config.write_text(json.dumps(CONFIG))

    def test_default_only_audits_read_only_without_loading_credentials(self):
        with patch.object(manager, "run_sql", return_value=json.dumps(CLEAN)) as query, \
             patch.object(manager, "load_passwords") as passwords:
            self.assertEqual(manager.main(["--config", str(self.config)]), 0)
        query.assert_called_once()
        self.assertTrue(query.call_args.args[0].startswith("BEGIN READ ONLY;"))
        passwords.assert_not_called()

    def test_clean_apply_performs_no_write_and_reads_no_credentials(self):
        with patch.object(manager, "run_sql", return_value=json.dumps(CLEAN)) as query, \
             patch.object(manager, "load_passwords") as passwords:
            self.assertEqual(manager.main(["--config", str(self.config), "--apply"]), 0)
        query.assert_called_once()
        passwords.assert_not_called()

    def test_drift_fails_check_without_applying(self):
        state = {**CLEAN, "issues": ["search_path differs: portal_bff"]}
        with patch.object(manager, "run_sql", return_value=json.dumps(state)) as query:
            self.assertEqual(manager.main(["--config", str(self.config)]), 1)
        query.assert_called_once()

    def test_does_not_accept_admin_duplicates_or_sql_identifiers(self):
        for role_names in (["postgres"], ["portal_bff", "portal_bff"], ["portal_x; SELECT 1"]):
            self.config.write_text(json.dumps({**CONFIG, "roles": role_names}))
            with self.assertRaises(ValueError):
                manager.load_config(self.config)

    def test_existing_roles_never_receive_a_password_statement(self):
        sql = manager.apply_sql(CONFIG, CLEAN, {})
        self.assertNotIn("PASSWORD", sql)
        self.assertNotIn("DROP", sql)
        self.assertIn("Role isolation postcondition failed", sql)
        self.assertIn("COMMIT;", sql)

    def test_new_role_needs_external_credentials_and_escapes_quotes(self):
        with self.assertRaises(ValueError):
            manager.load_passwords(None, ["portal_core"])
        password = "synthetic'credential\\only"
        source = self.root / "credentials.json"
        source.write_text(json.dumps({"portal_core": password}))
        source.chmod(0o600)
        values = manager.load_passwords(source, ["portal_core"])
        sql = manager.apply_sql(CONFIG, {**CLEAN, "existing_roles": ["portal_bff"]}, values)
        self.assertIn("PASSWORD 'synthetic''credential\\only'", sql)
        self.assertEqual(sql.count("PASSWORD"), 1)

    def test_credentials_reject_public_mode_and_symlink(self):
        source = self.root / "credentials.json"
        source.write_text(json.dumps({"portal_core": "synthetic-test-password"}))
        source.chmod(0o644)
        with self.assertRaises(ValueError):
            manager.load_passwords(source, ["portal_core"])
        source.chmod(0o600)
        link = self.root / "link.json"
        link.symlink_to(source)
        with self.assertRaises(OSError):
            manager.load_passwords(link, ["portal_core"])

    def test_blocker_prevents_any_apply_or_credential_read(self):
        state = {**CLEAN, "issues": ["blocked"], "blockers": ["unexpected schema owner"]}
        with patch.object(manager, "run_sql", return_value=json.dumps(state)) as query, \
             patch.object(manager, "load_passwords") as passwords:
            with self.assertRaises(ValueError):
                manager.main(["--config", str(self.config), "--apply"])
        query.assert_called_once()
        passwords.assert_not_called()


if __name__ == "__main__":
    unittest.main()
