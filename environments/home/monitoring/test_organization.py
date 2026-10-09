import importlib.util
from pathlib import Path
import sqlite3
import unittest

spec = importlib.util.spec_from_file_location('configure_org', Path(__file__).with_name('configure-organization.py'))
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)


class OrganizationTest(unittest.TestCase):
    def test_rename_preserves_identity_and_refuses_unexpected_state(self):
        with sqlite3.connect(':memory:') as connection:
            connection.executescript("CREATE TABLE org(id INTEGER PRIMARY KEY,name TEXT,updated TEXT); INSERT INTO org VALUES(1,'Main Org.','old'); CREATE TABLE dashboard(org_id INTEGER,title TEXT); INSERT INTO dashboard VALUES(1,'Existing dashboard');")
            config = {'id':1,'name':'UpdatingSpace LLC'}
            self.assertTrue(module.reconcile(connection, config)['drift'])
            self.assertEqual(connection.execute('SELECT name FROM org').fetchone()[0],'Main Org.')
            self.assertFalse(module.reconcile(connection, config, True)['drift'])
            self.assertEqual(connection.execute('SELECT * FROM dashboard').fetchall(),[(1,'Existing dashboard')])
            self.assertFalse(module.reconcile(connection, config)['drift'])
            connection.execute("UPDATE org SET name='Unrelated organization'")
            with self.assertRaises(AssertionError): module.reconcile(connection, config, True)


if __name__ == '__main__': unittest.main()
