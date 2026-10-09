import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import reconcile

ROOT = Path(__file__).parent
DESIRED = json.loads((ROOT/'desired-state.json').read_text())
SETTINGS = json.loads((ROOT/'bucket-settings.json').read_text())


class ReconcileTest(unittest.TestCase):
    def test_check_is_read_only_and_apply_changes_only_requested_fields(self):
        bucket = {'id': 'synthetic', 'keys': [], 'corsRules': []}
        calls = []
        def call(operation, params=None):
            calls.append((operation, params))
            if operation == 'UpdateBucket':
                self.assertEqual(set(params['body']), {'corsRules'})
                bucket.update(params['body'])
            return copy.deepcopy(bucket)
        alias = SETTINGS[0][1]
        with patch.object(reconcile, 'audit', return_value={alias: copy.deepcopy(bucket)}):
            self.assertEqual(len(reconcile.reconcile(DESIRED, SETTINGS[:1], call=call)), 1)
            self.assertEqual(calls, [])
            reconcile.reconcile(DESIRED, SETTINGS[:1], apply=True, call=call)
        self.assertEqual([name for name, _ in calls], ['GetBucketInfo', 'UpdateBucket', 'GetBucketInfo'])
        self.assertEqual(bucket['corsRules'][0]['AllowedMethod'], ['GET', 'HEAD', 'PUT'])
        with patch.object(reconcile, 'audit', return_value={alias: copy.deepcopy(bucket)}):
            self.assertEqual(reconcile.reconcile(DESIRED, SETTINGS[:1], apply=True, call=call), [])

    def test_unexpected_layout_and_unknown_cors_field_fail_closed(self):
        with self.assertRaises(RuntimeError):
            reconcile.audit(DESIRED, lambda *_: {'version': 999})
        settings = copy.deepcopy(SETTINGS[0][3])
        settings['CORSRules'][0]['UnknownSecurityField'] = True
        with self.assertRaises(RuntimeError):
            reconcile.settings_body('cors', settings)

    def test_owner_permission_drift_stops_before_any_update(self):
        layout = {'version': 1, 'stagedRoleChanges': [], 'stagedParameters': None,
                  'roles': [{'id': 'f4d2e55cbba93dd6', 'zone': 'dc1', 'capacity': 436 * 1024**3}]}
        names = DESIRED['principals']['updspace-id']['buckets']
        key = {'name': 'updspace-id', 'expired': False, 'permissions': {'createBucket': False},
               'buckets': [{'globalAliases': [name], 'localAliases': [],
                            'permissions': {'read': True, 'write': True, 'owner': True}} for name in names]}
        calls = []
        def call(operation, params=None):
            calls.append(operation)
            return layout if operation == 'GetClusterLayout' else key
        with self.assertRaisesRegex(RuntimeError, 'key permissions'):
            reconcile.reconcile(DESIRED, SETTINGS, apply=True, call=call)
        self.assertEqual(calls, ['GetClusterLayout', 'GetKeyInfo'])


if __name__ == '__main__':
    unittest.main()
