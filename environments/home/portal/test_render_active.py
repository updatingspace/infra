import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('render_active', Path(__file__).with_name('render-active.py'))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class RolloutTests(unittest.TestCase):
    def test_stage_has_nine_deployments_and_no_running_jobs(self):
        items = module.render()['items']
        deployments = [x for x in items if x['kind'] == 'Deployment']
        jobs = [x for x in items if x['kind'] == 'CronJob']
        self.assertEqual(len(deployments), 9)
        self.assertTrue(all(x['spec']['replicas'] == 1 for x in deployments))
        self.assertEqual(len(jobs), 14)
        self.assertTrue(all(x['spec']['suspend'] for x in jobs))
        outbox = next(x for x in jobs if x['metadata']['name'] == 'portal-outbox')
        self.assertEqual(outbox['spec']['schedule'], '*/15 * * * *')

    def test_frontend_keeps_no_privilege_escalation_or_extra_capabilities(self):
        deployment = next(x for x in module.render()['items'] if x['kind'] == 'Deployment' and x['metadata']['name'] == 'frontend')
        pod = deployment['spec']['template']['spec']
        container = pod['containers'][0]
        self.assertTrue(pod['securityContext']['runAsNonRoot'])
        self.assertFalse(container['securityContext']['allowPrivilegeEscalation'])
        self.assertTrue(container['securityContext']['readOnlyRootFilesystem'])
        self.assertEqual(container['securityContext']['capabilities'], {'drop': ['ALL']})
        self.assertIn('@sha256:', container['image'])

    def test_restore_override_stops_every_app_and_job(self):
        for item in module.render(dormant=True)['items']:
            if item['kind'] == 'Deployment': self.assertEqual(item['spec']['replicas'], 0)
            if item['kind'] == 'CronJob': self.assertTrue(item['spec']['suspend'])


if __name__ == '__main__': unittest.main()
