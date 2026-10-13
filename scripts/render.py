#!/usr/bin/env python3
"""Render the owned home manifests, without any secret values."""
import argparse
import importlib.util
import json
from pathlib import Path
import yaml
ROOT=Path(__file__).resolve().parents[1]
HOME=ROOT/'environments/home'
def render():
 spec=importlib.util.spec_from_file_location('monitoring',HOME/'monitoring/build.py')
 module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
 monitoring,_=module.build()
 objects=[]
 for name in ('edge/resources.json','monitoring/foundation.json'):
  objects.extend(json.loads((HOME/name).read_text())['items'])
 objects.extend(monitoring)
 for name in ('uptime-kuma/kuma.yaml','uptime-kuma/access.yaml','edge/monitoring-network.yaml','edge/portal-stage-network.yaml','uptime-kuma/portal-network.yaml','uptime-kuma/minecraft-network.yaml'):
  objects.extend(yaml.safe_load_all((HOME/name).read_text()))
 objects.append({'apiVersion':'v1','kind':'ConfigMap','metadata':{'name':'caddy-config','namespace':'edge'},'data':{'Caddyfile':(HOME/'edge/Caddyfile').read_text()}})
 identities=[(x['kind'],x['metadata'].get('namespace',''),x['metadata']['name']) for x in objects]
 assert len(identities)==len(set(identities)), 'Duplicate resource ownership'
 assert all(x['kind']!='Secret' for x in objects),'Secrets must stay outside Git'
 priority={'Namespace':0,'PersistentVolume':1,'PersistentVolumeClaim':2,'ConfigMap':3,'NetworkPolicy':4,'Service':5,'Deployment':6}
 return sorted(objects,key=lambda x:priority.get(x['kind'],3))
ACCESS_OBJECTS={('ConfigMap','edge','caddy-config'),('Deployment','edge','caddy'),('Deployment','observability','grafana'),('Deployment','observability','prometheus'),('Deployment','observability','alertmanager'),('NetworkPolicy','edge','caddy-to-monitoring'),('NetworkPolicy','observability','monitoring-from-caddy'),('Service','observability','grafana'),('Service','uptime-kuma','uptime-kuma'),('NetworkPolicy','observability','grafana-lan'),('NetworkPolicy','uptime-kuma','kuma-lan')}
if __name__=='__main__':
 parser=argparse.ArgumentParser();parser.add_argument('--scope',choices=('access','all'),default='access');args=parser.parse_args()
 objects=render()
 if args.scope=='access':objects=[o for o in objects if (o['kind'],o['metadata'].get('namespace'),o['metadata']['name']) in ACCESS_OBJECTS]
 print(json.dumps({'apiVersion':'v1','kind':'List','items':objects},indent=2))
