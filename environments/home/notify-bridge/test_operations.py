import base64,json,unittest,urllib.parse,tempfile,time,io
from pathlib import Path
from unittest.mock import patch
import vpn
from render import render

UUID='00000000-0000-4000-8000-000000000001'
def link(security='tls',kind='xhttp',extra=None):
    q={'security':security,'type':kind,'sni':'vpn.example','path':'/vpn','mode':'auto'}
    if extra is not None:q['extra']=json.dumps(extra)
    return ('vless://'+UUID+'@vpn.example:443?'+urllib.parse.urlencode(q)).encode()
class OperationsTests(unittest.TestCase):
    def test_subscription_conversion_and_credential_rotation(self):
        first=vpn.nodes(base64.b64encode(link()))[0]
        changed=link().replace(UUID.encode(),b'00000000-0000-4000-8000-000000000002')
        second=vpn.nodes(base64.b64encode(changed))[0]
        self.assertNotEqual(vpn.node_hash(first),vpn.node_hash(second))
        self.assertFalse(first['streamSettings']['tlsSettings']['allowInsecure'])
    def test_unencrypted_and_unknown_transport_rejected(self):
        for raw in [link('none'),link(kind='kcp'),b'invalid',link(extra={'downloadSettings':{'address':'127.0.0.1'}})]:
            with self.assertRaises((ValueError,UnicodeError)):vpn.nodes(raw)
    def test_proxy_has_explicit_final_deny(self):
        conf=vpn.config(vpn.nodes(link())[0]);self.assertEqual(conf['inbounds'][0]['listen'],'127.0.0.1')
        self.assertEqual(conf['routing']['rules'][0]['domain'],['full:api.telegram.org'])
        self.assertEqual(conf['routing']['rules'][-1]['outboundTag'],'block')
    def test_manifests_are_isolated_and_persistent(self):
        items=render({'bridge':'local/bridge:test','xray':'local/xray:test'})['items']
        for obj in items:
            if obj['kind']=='Deployment':
                pod=obj['spec']['template']['spec'];self.assertFalse(pod['automountServiceAccountToken']);self.assertTrue(pod['securityContext']['runAsNonRoot']);self.assertNotIn('hostNetwork',pod)
                self.assertEqual(obj['spec']['replicas'],1)
            if obj['kind']=='PersistentVolume':self.assertEqual(obj['spec']['persistentVolumeReclaimPolicy'],'Retain')
            if obj['kind']=='Service':self.assertNotIn('type',obj['spec'])
    def test_failed_node_switches_only_to_tested_replacement(self):
        raw=link()+b'\n'+link().replace(UUID.encode(),b'00000000-0000-4000-8000-000000000002')
        available=vpn.nodes(raw)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'subscription.raw').write_bytes(raw)
            (root/'vpn-state.json').write_text(json.dumps({'last_refresh':int(time.time()),'selected':vpn.node_hash(available[0]),'probe_failures':2}))
            (root/'xray.json').write_text(json.dumps(vpn.config(available[0])))
            with patch.object(vpn,'ROOT',root),patch('sys.argv',['vpn.py']),patch.object(vpn,'kube',return_value='10.43.1.2'),patch.object(vpn,'probe',side_effect=[False,True]),patch.object(vpn,'test_node',side_effect=[False,True]),patch.object(vpn,'apply_config') as apply:
                vpn.main()
            self.assertEqual(apply.call_count,1)
            state=json.loads((root/'vpn-state.json').read_text())
            self.assertEqual(state['selected'],vpn.node_hash(available[1]))
            self.assertTrue((root/'xray.previous.json').exists())
    def test_subscription_failure_preserves_working_configuration(self):
        raw=link();node=vpn.nodes(raw)[0]
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'subscription.raw').write_bytes(raw)
            (root/'subscription.url').write_text('https://example.test/subscription')
            previous=json.dumps(vpn.config(node));(root/'xray.json').write_text(previous)
            (root/'vpn-state.json').write_text(json.dumps({'last_refresh':0,'selected':vpn.node_hash(node)}))
            with patch.object(vpn,'ROOT',root),patch('sys.argv',['vpn.py']),patch.object(vpn.urllib.request,'urlopen',side_effect=OSError('offline')),patch.object(vpn,'kube',return_value='10.43.1.2'),patch.object(vpn,'probe',return_value=True),patch.object(vpn,'apply_config') as apply:
                vpn.main()
            apply.assert_not_called()
            self.assertEqual((root/'xray.json').read_text(),previous)
            self.assertEqual(json.loads((root/'vpn-state.json').read_text())['refresh_errors'],1)
    def test_connection_failures_refresh_recent_but_expired_credentials(self):
        raw=link();node=vpn.nodes(raw)[0]
        fresh=raw.replace(UUID.encode(),b'00000000-0000-4000-8000-000000000002')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'subscription.raw').write_bytes(raw)
            (root/'subscription.url').write_text('https://example.test/subscription')
            (root/'xray.json').write_text(json.dumps(vpn.config(node)))
            (root/'vpn-state.json').write_text(json.dumps({'last_refresh':int(time.time()),'selected':vpn.node_hash(node),'probe_failures':2}))
            with patch.object(vpn,'ROOT',root),patch('sys.argv',['vpn.py']),patch.object(vpn.urllib.request,'urlopen',return_value=io.BytesIO(fresh)) as fetch,patch.object(vpn,'kube',return_value='10.43.1.2'),patch.object(vpn,'probe',side_effect=[False,True]),patch.object(vpn,'test_node',return_value=True),patch.object(vpn,'apply_config') as apply:
                vpn.main()
            fetch.assert_called_once();apply.assert_called_once()
            self.assertEqual(json.loads((root/'vpn-state.json').read_text())['selected'],vpn.node_hash(vpn.nodes(fresh)[0]))
if __name__=='__main__':unittest.main()
