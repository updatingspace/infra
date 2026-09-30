#!/usr/bin/env python3
"""One-time migration. Run as root on /opt/pz-stack after staging/build tests."""
import datetime
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

os.chdir('/opt/pz-stack')
root=Path.cwd()

def command(args, **kw):
    return subprocess.run(args, check=True, **kw)

def output(args):
    return subprocess.check_output(args, text=True)

def log(s): print(s, flush=True)

state=json.loads(output(['docker','inspect','pz-b42-zomboid-1']))[0]
assert state['Config']['Image'].startswith('local/zomboid-control-panel-allinone:'), 'Already migrated or unexpected image'
assert state['State']['Running']
for image in ['local/pz-server:runtime-1','local/pz-panel:v1.3.7']:
    command(['docker','image','inspect',image],stdout=subprocess.DEVNULL)
spec=importlib.util.spec_from_file_location('game',root/'split-stage/runtime/game.py')
game=importlib.util.module_from_spec(spec);spec.loader.exec_module(game)
env=dict(x.split('=',1) for x in state['Config']['Env'])
os.environ['RCON_PASSWORD']=env['RCON_PASSWORD'];os.environ['RCON_PORT']='27015'
host=state['NetworkSettings']['Networks']['pz-b42_edge']['IPAddress']
players=game.rcon('players',host=host)
assert 'Players connected (0)' in players, 'Players online; migration deferred'
backup=root/'backups'/('split-'+datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
backup.mkdir(mode=0o700)
for file in ['.env','docker-compose.yml','Dockerfile','entrypoint.sh','Caddyfile']:
    shutil.copy2(root/file,backup/file)
    info=(root/file).stat();os.chown(backup/file,info.st_uid,info.st_gid)
shutil.copy2(root/'monitoring/otel-collector.yaml',backup/'otel-collector.yaml')
log('Rollback configuration saved: '+str(backup))
log('RCON save: '+game.rcon('save',host=host))
time.sleep(3)
try: log('RCON quit: '+game.rcon('quit',host=host))
except (ConnectionError,OSError): log('RCON connection closed after quit; checking game process exit')
for _ in range(180):
    processes=output(['docker','top',state['Id'],'-eo','pid,comm']).splitlines()[1:]
    if not any(p.split()[-1].startswith('ProjectZomboid') or p.split()[-1]=='java' for p in processes):
        log('Game process exited; stopping old panel container')
        break
    time.sleep(1)
else:
    raise RuntimeError('Game has not exited: leaving old container intact; no force stop')
command(['docker','compose','stop','zomboid'])
shutil.copy2(root/'data/panel/db.json',backup/'db.json')
# Preserve db owner for a documented rollback.
info=(root/'data/panel/db.json').stat();os.chown(backup/'db.json',info.st_uid,info.st_gid)
command(['python3',str(root/'split-stage/bin/migrate-panel-state.py')])
for directory in ['runtime','panel','bin','tests']:
    shutil.copytree(root/'split-stage'/directory,root/directory,dirs_exist_ok=True)
for file in ['docker-compose.yml','Caddyfile','README.md']:
    shutil.copy2(root/'split-stage'/file,root/file)
p=root/'monitoring/otel-collector.yaml'
s=p.read_text();assert 'http://zomboid:3001/api/health' in s
p.write_text(s.replace('http://zomboid:3001/api/health','http://panel:3001/api/health'))
command(['docker','compose','config','-q'])
command(['docker','compose','up','-d','--no-deps','--no-build','--pull','never','zomboid','panel'])
command(['docker','compose','up','-d','--no-deps','--no-build','--pull','never','caddy'])
command(['docker','compose','restart','otel-collector'])
log('Split containers started. Verify game readiness and panel independence. Rollback: '+str(backup))
