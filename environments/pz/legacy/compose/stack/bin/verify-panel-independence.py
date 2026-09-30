#!/usr/bin/env python3
"""Recreate ONLY panel while continuously checking the live game's RCON."""
import datetime
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import threading
import time

root=Path(__file__).resolve().parents[1]
os.chdir(root)
spec=importlib.util.spec_from_file_location('game',root/'runtime/game.py')
game=importlib.util.module_from_spec(spec);spec.loader.exec_module(game)
def inspect():
    p=json.loads(subprocess.check_output(['docker','inspect','pz-b42-zomboid-1']))[0]
    return p

def identity(p):
    processes=subprocess.check_output(['docker','top',p['Id'],'-eo','pid,comm'],text=True)
    jvm=[line.split()[0] for line in processes.splitlines()[1:] if line.split()[-1].startswith('ProjectZomboid')]
    return {'container_id':p['Id'],'started_at':p['State']['StartedAt'],'restart_count':p['RestartCount'],'game_pids':jvm}

p=inspect();before=identity(p)
env=dict(x.split('=',1) for x in p['Config']['Env'])
os.environ['RCON_PASSWORD']=env['RCON_PASSWORD'];os.environ['RCON_PORT']='27015'
host=p['NetworkSettings']['Networks']['pz-b42_edge']['IPAddress']
print('Preflight: '+game.rcon('players',host=host),flush=True)
stop=threading.Event();results=[]
def probe():
    while not stop.is_set():
        try:
            game.rcon('players',host=host)
            results.append({'ok':True})
        except Exception as e: results.append({'ok':False,'error_type':type(e).__name__})
        stop.wait(1)
worker=threading.Thread(target=probe);worker.start()
try:
    subprocess.run(['docker','compose','up','-d','--no-deps','--no-build','--pull','never','--force-recreate','--wait','--wait-timeout','120','panel'],check=True)
    time.sleep(5)
finally:
    stop.set();worker.join(timeout=35)
assert not worker.is_alive()
after=identity(inspect())
report={'checked_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'before':before,'after':after,'rcon_probes':len(results),'rcon_failures':[r for r in results if not r['ok']],'unchanged':before==after}
(root/'split-verification.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report,indent=2),flush=True)
assert before==after,'Game process/container changed during panel recreation'
assert results and all(r['ok'] for r in results),'RCON failed during panel recreation'
