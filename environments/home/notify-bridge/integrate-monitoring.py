#!/usr/bin/env python3
"""Patch live monitoring additively, validate candidates first, save root-only rollback."""
import base64,copy,json,os,subprocess,time
from pathlib import Path
import yaml
ROOT=Path('/opt/notify-bridge/private')

def kube(*args,data=None,timeout=60):
    r=subprocess.run(['k3s','kubectl','--cache-dir=/opt/notify-bridge/private/kube-cache',*args],input=data,capture_output=True,text=True,timeout=timeout)
    if r.returncode:raise RuntimeError('Kubernetes operation failed')
    return r.stdout

def get(kind,name):return json.loads(kube('-n','observability','get',kind,name,'-o','json'))
def backup(name,obj):
    p=ROOT/'rollback'/name;p.parent.mkdir(exist_ok=True,mode=0o700)
    if not p.exists():p.write_text(json.dumps(obj));p.chmod(0o600)
def replace_data(kind,name,old,data):
    patch=[{'op':'test','path':'/metadata/resourceVersion','value':old['metadata']['resourceVersion']},{'op':'replace','path':'/data','value':data}]
    kube('-n','observability','patch',kind,name,'--type=json','--patch-file=/dev/stdin',data=json.dumps(patch))
def validate(deploy,files,command):
    # File contents, including SMTP/webhook credentials, stay on stdin and in a temporary file.
    payload=''
    for path,content in files.items():
        encoded=base64.b64encode(content.encode()).decode()
        payload+='printf %s '+encoded+' | base64 -d > '+path+'\n'
    payload+=command+'\nresult=$?\nrm -f '+' '.join(files)+'\nexit "$result"\n'
    kube('-n','observability','exec','-i','deployment/'+deploy,'--','sh','-c','umask 077; sh',data=payload)

def main():
    os.umask(0o077)
    secret=get('secret','alertmanager-email');backup('alertmanager-email.json',secret)
    conf=yaml.safe_load(base64.b64decode(secret['data']['alertmanager.yml']))
    added=0
    for receiver in conf['receivers']:
        # Keep null/sink receivers silent and preserve existing email destinations/routes.
        if not receiver.get('email_configs'):continue
        webhooks=receiver.setdefault('webhook_configs',[])
        url='http://notify-bridge.notifications.svc.cluster.local:8080/webhooks/alertmanager'
        definition={'url':url,'send_resolved':True,'max_alerts':200,'http_config':{'authorization':{'type':'Bearer','credentials':(ROOT/'alertmanager-key').read_text().strip()}}}
        previous=next((w for w in webhooks if w.get('url')==url),None)
        if previous:previous.update(definition)
        else:webhooks.append(definition)
        added+=1
    if not added:raise RuntimeError('No email receiver found; routing requires review')
    candidate=yaml.safe_dump(conf,allow_unicode=True,sort_keys=False)
    validate('alertmanager',{'/tmp/notify-am.yml':candidate},'amtool check-config /tmp/notify-am.yml')
    data=dict(secret['data']);data['alertmanager.yml']=base64.b64encode(candidate.encode()).decode()
    if data!=secret['data']:
        replace_data('secret','alertmanager-email',secret,data)
        kube('-n','observability','rollout','restart','deployment/alertmanager')
        kube('-n','observability','rollout','status','deployment/alertmanager','--timeout=50s')
    cm=get('configmap','prometheus');backup('prometheus.json',cm)
    config=yaml.safe_load(cm['data']['prometheus.yml']);rules=yaml.safe_load(cm['data']['rules.yml'])
    job={'job_name':'notify-bridge','scrape_timeout':'5s','static_configs':[{'targets':['notify-bridge.notifications.svc.cluster.local:8080']}]}
    config['scrape_configs']=[j for j in config['scrape_configs'] if j['job_name']!='notify-bridge']+[job]
    specs=[
      ('NotifyBridgeUnavailable','up{job="notify-bridge"} == 0 or absent(up{job="notify-bridge"})','5m','critical','Приёмник уведомлений недоступен','notifications'),
      ('NotifyBridgeDeliveryBlocked','notify_bridge_pending > 0 and notify_bridge_oldest_pending_seconds > 120','3m','critical','Очередь уведомлений не доставляется в Telegram','notifications'),
      ('NotifyBridgeSenderDisabled','notify_bridge_sender_enabled == 0','15m','warning','Telegram-бот ещё не подключён','notifications'),
      ('TelegramTransportUnavailable','(notify_bridge_sender_enabled == 1) and (time() - notify_bridge_last_poll_timestamp_seconds > 180)','3m','critical','Нет подтверждённого соединения с Telegram','notifications'),
      ('NotifyBridgeHostMaintenanceStale','time() - notify_bridge_host_status_timestamp_seconds > 180','3m','critical','Не обновляется состояние фоновых задач уведомлений','notifications'),
      ('KumaReconciliationStale','time() - notify_bridge_kuma_reconcile_last_success_timestamp_seconds > 300','3m','critical','Не работает восстановление пропущенных событий Kuma','notifications'),
      ('VPNSubscriptionStale','time() - notify_bridge_vpn_last_refresh_timestamp_seconds > 86400','30m','warning','VPN-подписка не обновлялась больше суток','notifications'),
      ('VPNEgressProbeFailing','time() - notify_bridge_vpn_last_probe_timestamp_seconds > 300','3m','critical','VPN не проходит проверку доступа к Telegram','notifications'),
      ('IDBackupRecoveryPending','notify_bridge_id_backup_resume_pending == 1','10m','critical','Backup ID оставил незавершённое восстановление; требуется проверка','id-backup'),
      ('IDDeploymentUnavailable','kube_deployment_status_replicas_available{namespace="updspace-id",deployment="id"} < 1 or absent(kube_deployment_status_replicas_available{namespace="updspace-id",deployment="id"})','3m','critical','У ID нет доступной реплики, включая масштабирование до нуля','updspace-id')]
    group={'name':'notify-bridge','rules':[{'alert':n,'expr':expr,'for':duration,'labels':{'severity':sev,'notify_service':service,'component':'notifications' if service=='notifications' else 'id'},'annotations':{'summary':summary}} for n,expr,duration,sev,summary,service in specs]}
    rules['groups']=[g for g in rules['groups'] if g['name']!='notify-bridge']+[group]
    rule_text=yaml.safe_dump(rules,allow_unicode=True,sort_keys=False)
    validate_config=copy.deepcopy(config);validate_config['rule_files']=['/tmp/notify-rules.yml']
    validate('prometheus',{'/tmp/notify-prom.yml':yaml.safe_dump(validate_config),'/tmp/notify-rules.yml':rule_text},'promtool check config /tmp/notify-prom.yml')
    data=dict(cm['data']);data.update({'prometheus.yml':yaml.safe_dump(config,allow_unicode=True,sort_keys=False),'rules.yml':rule_text})
    if data!=cm['data']:
        replace_data('configmap','prometheus',cm,data)
        kube('-n','observability','rollout','restart','deployment/prometheus')
        kube('-n','observability','rollout','status','deployment/prometheus','--timeout=50s')
    print(json.dumps({'alertmanager_receivers':added,'new_rules':len(specs),'prometheus_scrape':'notify-bridge','rollback':'root-only snapshots saved'}))
if __name__=='__main__':
    try:main()
    except Exception as exc:raise SystemExit('Monitoring integration failed: '+type(exc).__name__)
