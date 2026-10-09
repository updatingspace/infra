import unittest
import json
import yaml
from render import render, HOME
class IaCTests(unittest.TestCase):
 def setUp(self):self.objects=render()
 def test_no_secrets_or_server_metadata(self):
  for o in self.objects:
   self.assertNotEqual(o['kind'],'Secret')
   self.assertNotIn('status',o)
   self.assertNotIn('managedFields',o['metadata'])
 def test_edge_routes_keep_existing_services_and_guard_all_monitoring(self):
  text=(HOME/'edge/Caddyfile').read_text()
  for host in ('status.updspace.com','storage.updspace.com','{$PANEL_DOMAIN}'):
   self.assertIn(host+' {',text)
  self.assertNotIn('basic_auth',text)
  self.assertIn('uri /oauth/observability-access',text)
  for host in ('grafana','prometheus','alerts'):
   block=text.split(host+'.updspace.com {',1)[1].split('\n}',1)[0]
   self.assertIn('import id_access',block)
   self.assertIn('header_up -Authorization',block)
  deployment=next(x for x in self.objects if x['kind']=='Deployment' and x['metadata']['name']=='caddy')
  env=deployment['spec']['template']['spec']['containers'][0]['env']
  self.assertFalse(any(x['name']=='MONITORING_AUTH_HASH' for x in env))
 def test_public_monitoring_ingress_is_only_caddy(self):
  policy=next(x for x in self.objects if x['metadata']['name']=='monitoring-from-caddy')
  peers=policy['spec']['ingress'][0]['from']
  self.assertEqual(len(peers),1)
  self.assertEqual(peers[0]['namespaceSelector']['matchLabels']['kubernetes.io/metadata.name'],'edge')
  self.assertEqual(peers[0]['podSelector']['matchLabels']['app.kubernetes.io/name'],'caddy')
  self.assertEqual({p['port'] for p in policy['spec']['ingress'][0]['ports']},{3000,9090,9093})
 def test_kuma_retains_data_and_authentication(self):
  pv=next(x for x in self.objects if x['kind']=='PersistentVolume' and x['metadata']['name']=='uptime-kuma-local')
  self.assertEqual(pv['spec']['persistentVolumeReclaimPolicy'],'Retain')
  deploy=next(x for x in self.objects if x['kind']=='Deployment' and x['metadata']['name']=='uptime-kuma')
  self.assertEqual(deploy['spec']['strategy']['type'],'Recreate')
 def test_adopted_host_files_are_complete(self):
  root=HOME/'host'
  for f in json.loads((root/'files.json').read_text()):
   self.assertTrue((root/f['source']).is_file(),f['source'])
   self.assertTrue(f['target'].startswith('/etc/'))
  config=yaml.safe_load((root/'k3s-config.yaml').read_text())
  self.assertEqual(config['node-name'],'updspace-home')
  self.assertNotIn('token',config)
 def test_retained_teamspeak_database_is_isolated_and_bounded(self):
  objects=list(yaml.safe_load_all((HOME/'teamspeak/mariadb.yaml').read_text()))
  db=next(o for o in objects if o['kind']=='StatefulSet')
  self.assertEqual(db['spec']['updateStrategy']['type'],'OnDelete')
  c=db['spec']['template']['spec']['containers'][0]
  self.assertIn('@sha256:',c['image'])
  self.assertEqual(c['resources']['limits']['memory'],'512Mi')
  policy=next(o for o in objects if o['kind']=='NetworkPolicy')
  self.assertNotIn('ingress',policy['spec'])
  self.assertNotIn('egress',policy['spec'])
  pv=next(o for o in objects if o['kind']=='PersistentVolume')
  self.assertEqual(pv['spec']['persistentVolumeReclaimPolicy'],'Retain')
 def test_adopted_kuma_pauses_and_tls_remain_enabled(self):
  config=json.loads((HOME/'uptime-kuma/config.json').read_text())
  paused={m['name'] for m in config['monitors'] if not m['active']}
  self.assertTrue({'is-schedule.updspace.com','ttnr.ru','updspace.com','spbetu.ru'} <= paused)
  self.assertFalse(config['settings']['disableAuth'])
  self.assertTrue(all(not m['ignoreTls'] for m in config['monitors']))
if __name__=='__main__':unittest.main()
