#!/usr/bin/env python3
"""Render only the resources owned by notify-bridge (JSON is valid Kubernetes input)."""
import json
from pathlib import Path
NS = 'notifications'

def label(name): return {'app.kubernetes.io/name': name}
def peer(ns, name): return {'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': ns}}, 'podSelector': {'matchLabels': label(name)}}
def port(number, protocol='TCP'): return {'port': number, 'protocol': protocol}
def policy(name, ns, pod, ingress=None, egress=None):
    spec = {'podSelector': {'matchLabels': label(pod)} if pod else {}, 'policyTypes': []}
    if ingress is not None: spec.update(ingress=ingress); spec['policyTypes'].append('Ingress')
    if egress is not None: spec.update(egress=egress); spec['policyTypes'].append('Egress')
    return {'apiVersion':'networking.k8s.io/v1','kind':'NetworkPolicy','metadata':{'name':name,'namespace':ns},'spec':spec}
def resources(cpu, memory, limit): return {'requests':{'cpu':cpu,'memory':memory,'ephemeral-storage':'16Mi'},'limits':{'cpu':'300m','memory':limit,'ephemeral-storage':'64Mi'}}
def container(name,image): return {'name':name,'image':image,'imagePullPolicy':'Never','securityContext':{'allowPrivilegeEscalation':False,'readOnlyRootFilesystem':True,'capabilities':{'drop':['ALL']}}}
def deployment(name,containers,volumes):
    return {'apiVersion':'apps/v1','kind':'Deployment','metadata':{'name':name,'namespace':NS},'spec':{'replicas':1,'revisionHistoryLimit':2,'strategy':{'type':'Recreate'},'selector':{'matchLabels':label(name)},'template':{'metadata':{'labels':label(name)},'spec':{'nodeSelector':{'kubernetes.io/hostname':'updspace-home'},'automountServiceAccountToken':False,'securityContext':{'runAsNonRoot':True,'runAsUser':10001,'runAsGroup':10001,'fsGroup':10001,'seccompProfile':{'type':'RuntimeDefault'}},'terminationGracePeriodSeconds':30,'containers':containers,'volumes':volumes}}}}
def render(images):
    items=[{'apiVersion':'v1','kind':'Namespace','metadata':{'name':NS,'labels':{'pod-security.kubernetes.io/enforce':'restricted'}}}]
    items += [{'apiVersion':'v1','kind':'PersistentVolume','metadata':{'name':'notify-bridge-data'},'spec':{'capacity':{'storage':'512Mi'},'accessModes':['ReadWriteOnce'],'persistentVolumeReclaimPolicy':'Retain','storageClassName':'','local':{'path':'/srv/notify-bridge/data'},'claimRef':{'namespace':NS,'name':'notify-bridge-data'},'nodeAffinity':{'required':{'nodeSelectorTerms':[{'matchExpressions':[{'key':'kubernetes.io/hostname','operator':'In','values':['updspace-home']}]}]}}}}, {'apiVersion':'v1','kind':'PersistentVolumeClaim','metadata':{'namespace':NS,'name':'notify-bridge-data'},'spec':{'accessModes':['ReadWriteOnce'],'storageClassName':'','volumeName':'notify-bridge-data','resources':{'requests':{'storage':'512Mi'}}}}]
    import copy
    pv=copy.deepcopy(items[1]);pv['metadata']['name']='notify-bridge-status';pv['spec']['local']['path']='/srv/notify-bridge/status';pv['spec']['capacity']['storage']='16Mi';pv['spec']['claimRef']['name']='notify-bridge-status'
    pvc=copy.deepcopy(items[2]);pvc['metadata']['name']='notify-bridge-status';pvc['spec']['volumeName']='notify-bridge-status';pvc['spec']['resources']['requests']['storage']='16Mi'
    items.extend([pv,pvc])
    c=container('bridge',images['bridge']);c.update(resources=resources('10m','32Mi','128Mi'), ports=[{'containerPort':8080,'name':'http'}],env=[{'name':'DB_PATH','value':'/data/bridge.db'},{'name':'HOST_STATUS_PATH','value':'/host-status/health.json'},{'name':'SOCKS_PROXY','value':'telegram-egress.notifications.svc.cluster.local:1080'}]+[{'name':key+'_FILE','value':'/run/secrets/'+file} for key,file in [('ALERTMANAGER_KEY','alertmanager-key'),('KUMA_KEY','kuma-key'),('TELEGRAM_TOKEN','telegram-token'),('TELEGRAM_CHAT','telegram-chat'),('TELEGRAM_USERS','telegram-users')]], volumeMounts=[{'name':'host-status','mountPath':'/host-status','readOnly':True},{'name':'data','mountPath':'/data'},{'name':'secrets','mountPath':'/run/secrets','readOnly':True}],readinessProbe={'httpGet':{'path':'/readyz','port':8080},'periodSeconds':10,'timeoutSeconds':3},livenessProbe={'httpGet':{'path':'/healthz','port':8080},'periodSeconds':20},startupProbe={'httpGet':{'path':'/readyz','port':8080},'periodSeconds':2,'failureThreshold':30})
    items.append(deployment('notify-bridge',[c],[{'name':'host-status','persistentVolumeClaim':{'claimName':'notify-bridge-status'}},{'name':'data','persistentVolumeClaim':{'claimName':'notify-bridge-data'}},{'name':'secrets','secret':{'secretName':'notify-bridge','defaultMode':0o440}}]))
    guard=container('guard',images['bridge']);guard.update(args=['--egress'],resources=resources('5m','16Mi','48Mi'),ports=[{'containerPort':1080,'name':'socks'}],readinessProbe={'tcpSocket':{'port':1080},'periodSeconds':5})
    vpn=container('xray',images['xray']);vpn.update(args=['run','-config','/run/vpn/config.json'],resources=resources('10m','32Mi','128Mi'),volumeMounts=[{'name':'vpn','mountPath':'/run/vpn','readOnly':True}])
    items.append(deployment('telegram-egress',[guard,vpn],[{'name':'vpn','secret':{'secretName':'telegram-egress-config','defaultMode':0o440}}]))
    for name,p in [('notify-bridge',8080),('telegram-egress',1080)]: items.append({'apiVersion':'v1','kind':'Service','metadata':{'name':name,'namespace':NS},'spec':{'selector':label(name),'ports':[{'name':'http' if p==8080 else 'socks','port':p,'targetPort':p}]}})
    dns={'to':[{'namespaceSelector':{'matchLabels':{'kubernetes.io/metadata.name':'kube-system'}},'podSelector':{'matchLabels':{'k8s-app':'kube-dns'}}}],'ports':[port(53,'UDP'),port(53)]}
    items += [policy('default-deny',NS,None,[],[]),policy('bridge',NS,'notify-bridge', ingress=[{'from':[peer('observability','alertmanager'),peer('observability','prometheus'),peer('uptime-kuma','uptime-kuma')],'ports':[port(8080)]}],egress=[dns,{'to':[peer(NS,'telegram-egress')],'ports':[port(1080)]}]),policy('egress',NS,'telegram-egress',ingress=[{'from':[peer(NS,'notify-bridge')],'ports':[port(1080)]}],egress=[dns,{'to':[{'ipBlock':{'cidr':'0.0.0.0/0','except':['0.0.0.0/8','10.0.0.0/8','100.64.0.0/10','127.0.0.0/8','169.254.0.0/16','172.16.0.0/12','192.168.0.0/16','224.0.0.0/4','240.0.0.0/4']}}],'ports':[port(443)]}])]
    for ns,name in [('observability','alertmanager'),('observability','prometheus'),('uptime-kuma','uptime-kuma')]: items.append(policy(name+'-to-notify-bridge',ns,name,egress=[{'to':[peer(NS,'notify-bridge')],'ports':[port(8080)]}]))
    return {'apiVersion':'v1','kind':'List','items':items}
if __name__=='__main__': print(json.dumps(render(json.loads((Path(__file__).parent/'images.json').read_text())),indent=2))
