import json
from pathlib import Path
import subprocess
import unittest
import yaml

ROOT = Path(__file__).resolve().parent


class MinecraftMonitorTests(unittest.TestCase):
    def test_game_protocol_and_private_destination_are_scoped(self):
        config = json.loads((ROOT/'config.json').read_text())
        monitor = next(m for m in config['monitors'] if m['name']=='Minecraft')
        self.assertEqual(monitor['type'], 'gamedig')
        self.assertEqual(monitor['game'], 'minecraft')
        self.assertTrue(monitor['gamedigGivenPortOnly'])
        self.assertEqual(monitor['port'], 25565)
        group = next(g for g in config['publicGroups'] if g['name']=='Игровые сервисы')
        self.assertIn({'name':'Minecraft','sendUrl':False}, group['monitors'])
        policy = yaml.safe_load((ROOT/'minecraft-network.yaml').read_text())
        rule = policy['spec']['egress'][0]
        self.assertEqual(rule['ports'], [{'protocol':'TCP','port':25565}])
        self.assertEqual(rule['to'], [{'namespaceSelector':{'matchLabels':{'kubernetes.io/metadata.name':'minecraft'}},
                                      'podSelector':{'matchLabels':{'app.kubernetes.io/name':'minecraft'}}}])

    def test_configuration_rejects_undeclared_game_destinations_before_connecting(self):
        # Execute only the preflight section; no Kuma session or credentials are used.
        source = (ROOT/'config.cjs').read_text().split('const socket =')[0]
        program = r'''
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=JSON.parse(process.argv[1]);
const desired=JSON.parse(fs.readFileSync(process.argv[2]));
function validate(config) {
    vm.runInNewContext(source, {URL,process:{argv:['node','config.cjs','fixture.json']},require(name) {
        if(name==='node:fs') return {readFileSync(path){return JSON.stringify(path===0?{}:config);}};
        if(name==='/app/node_modules/socket.io-client') return {io(){throw Error('Unexpected connection');}};
        return require(name);
    }});
}
validate(desired);
const index=desired.monitors.findIndex(m=>m.name==='Minecraft');
for(const change of [{game:'quake'},{port:25566},{hostname:'example.invalid'},{gamedigGivenPortOnly:false},{type:'port'}]) {
    const invalid=structuredClone(desired);Object.assign(invalid.monitors[index],change);
    assert.throws(()=>validate(invalid));
}
'''
        subprocess.run(['node','-e',program,json.dumps(source),str(ROOT/'config.json')], check=True)


if __name__ == '__main__': unittest.main()
