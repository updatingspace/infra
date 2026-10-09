#!/usr/bin/env python3
"""Import exact legacy images on the VM and write credential-free tfvars."""
import importlib.util
import json
from pathlib import Path
import re
import shlex

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("deploy", ROOT / "deploy.py")
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)
platform = (ROOT / "platform/terraform.tfvars.example").read_text()
platform = re.sub(r'^kubeconfig_path\s*=.*$', 'kubeconfig_path = "/etc/rancher/k3s/operator.yaml"', platform, flags=re.M)
platform = re.sub(r'^kubeconfig_context\s*=.*$', 'kubeconfig_context = "default"', platform, flags=re.M)
REMOTE = r'''
import json,os,pathlib,subprocess,sys
os.umask(0o077)
p=json.load(sys.stdin); root=pathlib.Path('/opt/pz-infrastructure')
images={}
for old,repository in [('zomboid','local/pz-server'),('panel','local/pz-panel'),('caddy','local/caddy'),('otel-collector','local/otel-collector')]:
    info=json.loads(subprocess.check_output(['docker','inspect',f'pz-b42-{old}-1']))[0]
    imageid=info['Image']; ref='docker.io/'+repository+':migration-'+imageid.split(':')[1][:16]
    subprocess.run(['docker','tag',imageid,ref],check=True)
    save=subprocess.Popen(['docker','save',ref],stdout=subprocess.PIPE)
    imported=subprocess.run(['k3s','ctr','images','import','-'],stdin=save.stdout,stdout=subprocess.DEVNULL)
    save.stdout.close()
    assert save.wait()==0 and imported.returncode==0, 'Image import failed'
    images[old]={'reference':ref,'docker_config_digest':imageid}
    print(f'Imported unchanged {old} image as {ref}',flush=True)
(root/'images.json').write_text(json.dumps(images,indent=2)+'\n')
common={'kubeconfig_path':'/etc/rancher/k3s/operator.yaml','kubeconfig_context':'default'}
(root/'platform/current.auto.tfvars').write_text(p['platform'])
(root/'workloads/current.auto.tfvars.json').write_text(json.dumps(dict(common,game_image=images['zomboid']['reference'],panel_image=images['panel']['reference'],caddy_image=images['caddy']['reference']),indent=2)+'\n')
(root/'observability/current.auto.tfvars.json').write_text(json.dumps(dict(common,collector_image=images['otel-collector']['reference']),indent=2)+'\n')
for item in root.glob('*/current.auto.tfvars*'): item.chmod(0o600)
'''
deploy.ssh("sudo -n python3 -c " + shlex.quote(REMOTE), input=json.dumps({"platform": platform}), text=True)
