#!/usr/bin/env python3
"""Validate resolved Compose without printing credentials. Run on the stack host."""
import json
import os
from pathlib import Path
import subprocess

os.chdir(Path(__file__).resolve().parents[1])
cmd=['docker','compose','config','--format','json']
a=json.loads(subprocess.check_output(cmd))
b=json.loads(subprocess.check_output(cmd,env={**os.environ,'PANEL_REF':'v999.999.999'}))
assert a['services']['zomboid']==b['services']['zomboid'], 'Panel version changes game config'
assert a['services']['panel']!=b['services']['panel'], 'Panel version is not applied'
assert not a['services']['zomboid'].get('depends_on'), 'Game depends on another service'
assert a['services']['zomboid']['build']['context']!=a['services']['panel']['build']['context']
assert not any('docker.sock' in str(v) for v in a['services']['panel']['volumes'])
print('PASS: independent build contexts, panel version cannot change game, game has no service dependencies, panel has no Docker control socket')
