import copy
import unittest

import prepare_cutover as c
from snapshot_source import is_id


def fixture():
    integration = {'type': 'serverless_containers', 'container_id': next(iter(c.REVISIONS))}
    source = {'openapi': '3.0.0', 'info': {'title': 'ID', 'version': '1'}, 'servers': [],
              'paths': {f'/route-{i}': {method: {c.INTEGRATION: integration, 'responses': {'200': {}}}
                        for method in (['get', 'post'] if i < 50 else ['get'])} for i in range(81)}}
    inventory = {'stamp': 'fixture', 'gateway': {'id': c.GATEWAY}, 'folder_bindings': [],
        'containers': [{'id': cid, 'revisions': [{'id': rid, 'status': 'ACTIVE', 'execution_timeout': '600s'}],
                        'bindings': [{'role_id': 'serverless.containers.invoker',
                                      'subject': {'type': 'serviceAccount', 'id': 'test-caller'}}]}
                       for cid, rid in c.REVISIONS.items()],
        'triggers': [{'id': tid, 'status': 'ACTIVE', 'rule': {'timer': {
            'invoke_container_with_retry': {'container_id': 'bba9v82d1op2qbh2kqtt'}}}}
            for tid in sorted(c.TRIGGERS)]}
    return inventory, source


class CutoverTests(unittest.TestCase):
    def test_all_routes_fenced_and_source_unchanged(self):
        inventory, source = fixture()
        original = copy.deepcopy(source)
        result, plan = c.prepare(inventory, [c.DB_BINDING], [], source)
        self.assertEqual(source, original)
        self.assertEqual(sum(len(x) for x in result['paths'].values()), 131)
        for path in result['paths'].values():
            for operation in path.values():
                self.assertEqual(operation[c.INTEGRATION]['type'], 'dummy')
                self.assertEqual(operation[c.INTEGRATION]['http_code'], 503)
                self.assertNotIn('container_id', operation[c.INTEGRATION])
        self.assertFalse(plan['applied'])
        self.assertEqual(plan['freeze']['drain_seconds_after_verified_invocation_fence'], 600)
        self.assertEqual(len(plan['freeze']['remove_direct_invocation_bindings']), 5)

    def test_paused_trigger_stays_paused_on_rollback(self):
        inventory, source = fixture()
        inventory['triggers'][0]['status'] = 'PAUSED'
        _, plan = c.prepare(inventory, [c.DB_BINDING], [], source)
        resume = plan['rollback_before_target_accepts_writes']['resume_previously_active_triggers']
        self.assertEqual(len(resume), 4)
        self.assertNotIn(inventory['triggers'][0]['id'], [x[-1] for x in resume])

    def test_changed_source_and_inherited_permissions_rejected(self):
        for change in ('revision', 'trigger', 'database', 'folder', 'gateway'):
            inventory, source = fixture()
            bindings = [c.DB_BINDING]
            if change == 'revision':
                inventory['containers'][0]['revisions'][0]['id'] = 'new-release'
            elif change == 'trigger':
                inventory['triggers'][0]['rule']['timer']['invoke_container_with_retry']['container_id'] = 'portal'
            elif change == 'database':
                bindings = []
            elif change == 'folder':
                inventory['folder_bindings'] = [{'role_id': 'ydb.editor', 'subject': {'id': c.RUNTIME_ACCOUNT}}]
            else:
                source['paths']['/route-0']['get'][c.INTEGRATION]['container_id'] = 'portal'
            with self.subTest(change=change), self.assertRaises(ValueError):
                c.prepare(inventory, bindings, [], source)

    def test_scope_does_not_select_portal_or_similarly_named_projects(self):
        self.assertTrue(is_id('updspace-id'))
        self.assertTrue(is_id('updatingspace-id-backend'))
        self.assertFalse(is_id('updspace-identity'))
        self.assertFalse(is_id('updspace-portal-id-client'))


if __name__ == '__main__':
    unittest.main()
