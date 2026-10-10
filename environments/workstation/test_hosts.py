import importlib.util
from pathlib import Path
import unittest
spec = importlib.util.spec_from_file_location('hosts',Path(__file__).with_name('apply-monitoring-hosts.py'))
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
class HostsTests(unittest.TestCase):
    def test_only_managed_block_changes(self):
        original='127.0.0.1 localhost\n192.0.2.1 keep.example\n'
        block=Path(__file__).with_name('hosts-monitoring.block').read_text()
        result=module.updated_hosts(original,block)
        self.assertTrue(result.startswith(original))
        self.assertEqual(module.updated_hosts(result,block), result)
        with self.assertRaises(ValueError):module.updated_hosts(result+block,block)
