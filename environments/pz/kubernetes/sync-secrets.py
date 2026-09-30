#!/usr/bin/env python3
"""Copy legacy environment values to Kubernetes Secrets entirely on the VM."""
import importlib.util
from pathlib import Path
import shlex

spec = importlib.util.spec_from_file_location("deploy", Path(__file__).with_name("deploy.py"))
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)
REMOTE = r'''
import json,pathlib,subprocess
root=pathlib.Path('/opt/pz-stack/backups/k3s-migration-20260930')
for old,namespace,name in [('zomboid','zomboid','pz-runtime'),('panel','zomboid','pz-panel'),('caddy','edge','caddy-runtime'),('otel-collector','observability','monium-env')]:
    c=json.loads((root/(old+'-inspect.json')).read_text())
    values=dict(item.split('=',1) for item in c['Config']['Env'])
    obj={'apiVersion':'v1','kind':'Secret','metadata':{'namespace':namespace,'name':name},'type':'Opaque','stringData':values}
    result=subprocess.run(['k3s','kubectl','--kubeconfig','/etc/rancher/k3s/operator.yaml','apply','--server-side','--field-manager=pz-migration','-f','-'],input=json.dumps(obj),text=True,capture_output=True)
    if result.returncode:
        # Do not print CLI diagnostics that could contain serialized values.
        raise SystemExit(f'Secret import failed for {namespace}/{name}; inspect locally on VM')
    print(f'Secret {namespace}/{name} reconciled on server (values not exported).')
'''
deploy.ssh("sudo -n python3 -c " + shlex.quote(REMOTE))
