#!/usr/bin/env python3
"""Copy legacy environment values to Kubernetes Secrets entirely on the VM."""
import importlib.util
from pathlib import Path
import shlex

spec = importlib.util.spec_from_file_location("deploy", Path(__file__).with_name("deploy.py"))
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)
PIN_OVERRIDE = r'''
import json, os, pathlib, shlex, stat

STORM_AGENT = '-javaagent:/zomboid/Workshop/storm/Contents/mods/storm/bootstrap/storm-bootstrap.jar'

def require_storm_trusted_path(path):
    for component in [*reversed(path.parents), path]:
        info = component.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise RuntimeError('Storm override path must be controlled by root')


def apply_storm_pin(values, path=pathlib.Path('/etc/pz-runtime/storm-pin.json'), current_options=None):
    """Keep the reviewed local framework pin across legacy Secret imports."""
    if not path.exists():
        if path.is_symlink() or (current_options and STORM_AGENT in shlex.split(current_options)):
            raise RuntimeError('Existing local Storm pin requires its trusted override file')
        return values
    require_storm_trusted_path(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 16384:
            raise RuntimeError('Storm override must be a private bounded regular file')
        override = json.load(source)
    if not isinstance(override, dict) or set(override) != {'JAVA_TOOL_OPTIONS'}:
        raise RuntimeError('Storm override may replace only JAVA_TOOL_OPTIONS')
    options = override['JAVA_TOOL_OPTIONS']
    if not isinstance(options, str) or any(x in options for x in ('\n', '\r', '\0')):
        raise RuntimeError('Invalid Storm override options')
    tokens = shlex.split(options)
    if tokens.count(STORM_AGENT) != 1 or tokens.count('-DstormType=local') != 1 or tokens.count('-Dstorm.core.updateUrl=') != 1:
        raise RuntimeError('Storm override must preserve the reviewed local pin and disabled CDN update')
    if any(x.startswith('-DstormType=') and x != '-DstormType=local' for x in tokens) or any(x.startswith('-Dstorm.core.updateUrl=') and x != '-Dstorm.core.updateUrl=' for x in tokens):
        raise RuntimeError('Conflicting Storm override options')
    return {**values, 'JAVA_TOOL_OPTIONS': options}
'''

REMOTE = PIN_OVERRIDE + r'''
import base64,json,pathlib,subprocess
root=pathlib.Path('/opt/pz-stack/backups/k3s-migration-20260930')
for old,namespace,name in [('zomboid','zomboid','pz-runtime'),('panel','zomboid','pz-panel'),('caddy','edge','caddy-runtime'),('otel-collector','observability','monium-env')]:
    c=json.loads((root/(old+'-inspect.json')).read_text())
    values=dict(item.split('=',1) for item in c['Config']['Env'])
    if name=='pz-runtime':
        current=subprocess.run(['k3s','kubectl','--kubeconfig','/etc/rancher/k3s/operator.yaml','-n',namespace,'get','secret',name,'--ignore-not-found','-o','json'],capture_output=True,text=True,timeout=30)
        if current.returncode: raise SystemExit('Cannot inspect existing Storm pin before Secret import')
        current_data=json.loads(current.stdout).get('data',{}) if current.stdout.strip() else {}
        current_options=base64.b64decode(current_data.get('JAVA_TOOL_OPTIONS','')).decode()
        values=apply_storm_pin(values,current_options=current_options)
    obj={'apiVersion':'v1','kind':'Secret','metadata':{'namespace':namespace,'name':name},'type':'Opaque','stringData':values}
    result=subprocess.run(['k3s','kubectl','--kubeconfig','/etc/rancher/k3s/operator.yaml','apply','--server-side','--field-manager=pz-migration','-f','-'],input=json.dumps(obj),text=True,capture_output=True)
    if result.returncode:
        # Do not print CLI diagnostics that could contain serialized values.
        raise SystemExit(f'Secret import failed for {namespace}/{name}; inspect locally on VM')
    print(f'Secret {namespace}/{name} reconciled on server (values not exported).')
'''
if __name__ == "__main__":
    deploy.ssh("sudo -n python3 -c " + shlex.quote(REMOTE))
