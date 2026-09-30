"""Check the deployment wrapper's Docker targets without using a Docker daemon."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

class PanelUpdateTests(unittest.TestCase):
    def exercise(self, present):
        with tempfile.TemporaryDirectory(prefix='pz-panel-wrapper-') as directory:
            root=Path(directory)
            (root/'bin').mkdir();(root/'fake').mkdir()
            script=root/'bin/update-panel.sh'
            shutil.copy2(Path(__file__).parents[1]/'bin/update-panel.sh',script)
            (root/'.env').write_text('PANEL_REF=v1.3.7\nKEEP_THIS=fixture\n')
            (root/'.env').chmod(0o600)
            fake=root/'fake/docker'
            fake.write_text('''#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
args=sys.argv[1:]
with open(os.environ['TEST_CALLS'],'a') as f:f.write(json.dumps(args)+'\\n')
if args==['compose','ps','-a','-q','zomboid']:
    if os.environ['TEST_GAME_PRESENT']=='1':print('fixture-game')
elif args[0]=='inspect':print('fixture-game same-start-time 0')
elif args==['compose','build','panel']:pass
elif args==['compose','up','-d','--no-deps','--no-build','--pull','never','--wait','--wait-timeout','120','panel']:pass
else:sys.exit('Unexpected Docker operation: '+repr(args))
''')
            fake.chmod(0o755)
            env={**os.environ,'PATH':str(root/'fake')+':'+os.environ['PATH'],'TEST_CALLS':str(root/'calls.jsonl'),'TEST_GAME_PRESENT':'1' if present else '0'}
            subprocess.run(['bash',str(script),'v1.3.8'],env=env,check=True,capture_output=True,text=True)
            self.assertEqual((root/'.env').read_text(),'PANEL_REF=v1.3.8\nKEEP_THIS=fixture\n')
            self.assertEqual((root/'.env').stat().st_mode & 0o777,0o600)
            calls=[json.loads(line) for line in (root/'calls.jsonl').read_text().splitlines()]
            mutations=[a for a in calls if a[:2] in [['compose','build'],['compose','up']]]
            self.assertEqual(len(mutations),2)
            self.assertTrue(all(a[-1]=='panel' for a in mutations))

    def test_game_present_is_unchanged(self):self.exercise(True)
    def test_game_absent_is_not_started(self):self.exercise(False)

if __name__=='__main__':unittest.main()
