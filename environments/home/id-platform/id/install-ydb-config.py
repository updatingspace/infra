#!/usr/bin/env python3
"""Check/install the versioned server config only on an initialized, stopped YDB."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import yaml

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--apply', action='store_true')
args = parser.parse_args()
source = Path(__file__).with_name('ydb-config.yaml')
target = Path('/srv/updspace/id-ydb/cluster/kikimr_configs/config.yaml')
config = yaml.safe_load(source.read_text())
assert config['domains_config']['security_config']['enforce_user_token_requirement'] is True
assert config['auth_config']['use_builtin_domain'] is False
assert 'default_users' not in config['domains_config']['security_config'], 'Bootstrap passwords must not enter IaC'
if not Path('/srv/updspace/id-ydb/.authentication-enforced').is_file():
    raise SystemExit('Initialized authenticated volume required; this is not a bootstrap tool')
if source.read_bytes() == target.read_bytes():
    print('YDB config matches')
elif not args.apply:
    raise SystemExit('YDB config differs; check is read-only')
else:
    command = ['k3s', 'kubectl', '-n', 'updspace-data']
    sts = json.loads(subprocess.check_output(command + ['get', 'statefulset/id-ydb', '-o', 'json']))
    pods = json.loads(subprocess.check_output(command + ['get', 'pods', '-l', 'app.kubernetes.io/name=id-ydb', '-o', 'json']))
    if sts['spec']['replicas'] != 0 or pods['items']:
        raise SystemExit('Refusing to replace config while YDB is running')
    temporary = target.with_suffix('.yaml.new')
    temporary.write_bytes(source.read_bytes())
    os.chown(temporary, 65534, 65534)
    os.chmod(temporary, 0o600)
    temporary.replace(target)
    print('YDB config installed; restart remains an explicit operator action')
