#!/usr/bin/env python3
"""Render the single-node local monitoring stack; credentials are supplied separately."""
import json
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parent
NS = 'observability'
IMAGES = {
    'prometheus': 'docker.io/prom/prometheus:v3.15.0@sha256:efd719c99d83b060d9daefdcf00360461adf279f45ef5391f8d111892118753e',
    'grafana': 'docker.io/grafana/grafana:13.2.3@sha256:b28bae15e219c998fb0e0424ed724930cc61b1f61fb404d47c862f9a23f9e572',
    'loki': 'docker.io/grafana/loki:3.7.8@sha256:1107dd5274e0ada47e42472b7a7e71f3b2a2fe878878108f3e2f9e51528f0193',
    'alertmanager': 'docker.io/prom/alertmanager:v0.34.1@sha256:e9733bafb1bdef9b00e25a21f8f99dc26a22224bf16641ad754d1649f4c3357a',
}

def build():
    resources = []
    def config(name, data):
        resources.append({'apiVersion':'v1','kind':'ConfigMap','metadata':{'name':name,'namespace':NS},'data':data})
    def app(name, port, uid, memory, cpu, args, config_files, probe):
        labels = {'app.kubernetes.io/name': name}
        mounts = [{'name':'data','mountPath':'/var/lib/app'}]
        volumes = [{'name':'data','hostPath':{'path':f'/srv/pz-monitoring/{name}','type':'Directory'}}]
        for index,(config_name,key,path) in enumerate(config_files):
            volume = 'config'+str(index)
            mounts.append({'name':volume,'mountPath':path,'subPath':key,'readOnly':True})
            volumes.append({'name':volume,'configMap':{'name':config_name}})
        container = {'name':name,'image':IMAGES[name],'imagePullPolicy':'IfNotPresent','args':args,
            'ports':[{'name':'http','containerPort':port}], 'volumeMounts':mounts,
            'securityContext':{'allowPrivilegeEscalation':False,'capabilities':{'drop':['ALL']}},
            'resources':{'requests':{'cpu':'50m','memory':'128Mi','ephemeral-storage':'64Mi'},
                         'limits':{'cpu':cpu,'memory':memory,'ephemeral-storage':'512Mi'}},
            'readinessProbe':{'httpGet':{'path':probe,'port':'http'},'periodSeconds':10,'timeoutSeconds':5},
            'startupProbe':{'httpGet':{'path':probe,'port':'http'},'periodSeconds':10,'failureThreshold':60}}
        if name == 'grafana':
            container['env'] = [{'name':'GF_PATHS_DATA','value':'/var/lib/app'},
                {'name':'GF_ANALYTICS_REPORTING_ENABLED','value':'false'},
                {'name':'GF_ANALYTICS_CHECK_FOR_UPDATES','value':'false'},
                {'name':'GF_USERS_ALLOW_SIGN_UP','value':'false'},
                {'name':'GF_SERVER_ROOT_URL','value':'https://grafana.updspace.com'},
                {'name':'GF_AUTH_ANONYMOUS_ENABLED','value':'false'},
                {'name':'GF_SECURITY_COOKIE_SECURE','value':'true'},
                {'name':'GF_SECURITY_ADMIN_USER','value':'admin'},
                {'name':'GF_SECURITY_ADMIN_PASSWORD','valueFrom':{'secretKeyRef':{'name':'grafana-admin','key':'password'}}}]
        resources.append({'apiVersion':'apps/v1','kind':'Deployment','metadata':{'name':name,'namespace':NS},
            'spec':{'replicas':1,'strategy':{'type':'Recreate'},'selector':{'matchLabels':labels},
                'template':{'metadata':{'labels':labels},'spec':{'nodeSelector':{'kubernetes.io/hostname':'updspace-home'},
                    'automountServiceAccountToken':False,'securityContext':{'runAsNonRoot':True,'runAsUser':uid,'runAsGroup':uid,'seccompProfile':{'type':'RuntimeDefault'}},
                    'containers':[container],'volumes':volumes,'terminationGracePeriodSeconds':60}}}})
        service = {'type':'ClusterIP','selector':labels,'ports':[{'name':'http','port':port,'targetPort':'http'}]}
        if name == 'grafana':
            service['type']='NodePort'; service['ports'][0]['nodePort']=30030
            service['externalTrafficPolicy']='Local'
        resources.append({'apiVersion':'v1','kind':'Service','metadata':{'name':name,'namespace':NS},'spec':service})

    rules = {'groups':[{'name':'pz-local','rules':[
        {'alert':'PZUnavailable','expr':'up{job="zomboid"} == 0','for':'15m','labels':{'severity':'critical'},'annotations':{'summary':'Zomboid metrics are unavailable'}},
        {'alert':'PZPanelUnavailable','expr':'max(httpcheck_status{check_name="panel-http",http_status_class="2xx"}) == 0','for':'15m','labels':{'severity':'critical'},'annotations':{'summary':'Panel health check is failing'}},
        {'alert':'PZTPSDegraded','expr':'sum(rate(storm_server_tick_total[5m])) / max(storm_server_lock_fps) < 0.8','for':'5m','labels':{'severity':'warning'},'annotations':{'summary':'Game tick rate is below 80 percent of its configured target'}},
        {'alert':'PZTPSCritical','expr':'sum(rate(storm_server_tick_total[5m])) / max(storm_server_lock_fps) < 0.5','for':'5m','labels':{'severity':'critical'},'annotations':{'summary':'Game tick rate is below 50 percent of its configured target'}},
        {'alert':'PZHeapHigh','expr':'sum(jvm_memory_used_bytes{area="heap"}) / sum(jvm_memory_max_bytes{area="heap"}) > 0.85','for':'10m','labels':{'severity':'warning'},'annotations':{'summary':'Game heap usage exceeds 85 percent'}},
        {'alert':'PZHeapCritical','expr':'sum(jvm_memory_used_bytes{area="heap"}) / sum(jvm_memory_max_bytes{area="heap"}) > 0.95','for':'10m','labels':{'severity':'critical'},'annotations':{'summary':'Game heap usage exceeds 95 percent'}},
        {'alert':'PZDiskSpaceLow','expr':'system_filesystem_utilization_ratio > 0.8','for':'10m','labels':{'severity':'warning'},'annotations':{'summary':'Filesystem usage exceeds 80 percent'}},
        {'alert':'PZDiskSpaceCritical','expr':'system_filesystem_utilization_ratio > 0.9','for':'10m','labels':{'severity':'critical'},'annotations':{'summary':'Filesystem usage exceeds 90 percent'}},
    ]}]}
    # Preserve the existing backup contracts, including missing-series alarms.
    backup_checks = [
        ('ExporterUnavailable', '1 - min(up{job="pz-backup"})', '>', None, 0.9, '60m', True),
        ('Stale', 'max(pz_backup_age_seconds)', '>', 93600, 108000, '10m', True),
        ('Failed', 'max({__name__=~"pz_backup_current_job_failed|pz_backup_state_file_invalid|pz_backup_verification_failed|pz_backup_capture_failed|pz_backup_restore_failed"})', '>', None, 0.9, '5m', True),
        ('MountMissing', '1 - min(pz_backup_mounted{filesystem=~"data|spool"})', '>', None, 0.9, '15m', True),
        ('SpaceLow', 'min(pz_backup_free_bytes)', '<', 4294967296, 2147483648, '5m', True),
        ('InodesLow', 'min(pz_backup_free_inodes)', '<', 20000, 10000, '5m', True),
        ('MultipartIncomplete', 'max(pz_backup_incomplete_multipart)', '>', None, 0.9, '15m', False),
        ('Stalled', 'max(pz_backup_incomplete) * (1 - max(pz_backup_capture_failed)) * max(pz_backup_stage_age_seconds)', '>', 3600, 7200, '10m', False),
        ('RestoreStale', 'max(pz_backup_restore_age_seconds)', '>', 2592000, 3196800, '10m', False),
    ]
    for name, expression, comparison, warning, critical, window, missing_alarm in backup_checks:
        for severity, threshold in [('warning', warning), ('critical', critical)]:
            if threshold is None:
                continue
            query = f'({expression}) {comparison} {threshold}'
            if severity == 'critical' and missing_alarm:
                query += f' or absent({expression})'
            rules['groups'][0]['rules'].append({'alert': 'PZBackup' + name + severity.title(),
                'expr': query, 'for': window, 'labels': {'severity': severity},
                'annotations': {'summary': 'PZ backup: ' + name}})
    prom = {'global':{'scrape_interval':'30s','evaluation_interval':'30s'},'rule_files':['/etc/config/rules.yml'],
        'alerting':{'alertmanagers':[{'static_configs':[{'targets':['alertmanager:9093']}]}]},
        'scrape_configs':[{'job_name':job,'static_configs':[{'targets':[target]}]} for job,target in [
            ('prometheus','localhost:9090'),('zomboid','zomboid.zomboid.svc.cluster.local:9090'),
            ('collector','otel-collector:8888'),('pz-backup','192.168.1.176:9109')]]}
    config('prometheus',{'prometheus.yml':yaml.safe_dump(prom),'rules.yml':yaml.safe_dump(rules)})
    alert = {'route':{'receiver':'local-ui','group_by':['alertname'],'group_wait':'30s','group_interval':'5m','repeat_interval':'4h'},'receivers':[{'name':'local-ui'}]}
    config('alertmanager',{'alertmanager.yml':yaml.safe_dump(alert)})
    loki={'auth_enabled':False,'server':{'http_listen_port':3100},
        'common':{'path_prefix':'/var/lib/app','replication_factor':1,'ring':{'kvstore':{'store':'inmemory'}},'storage':{'filesystem':{'chunks_directory':'/var/lib/app/chunks','rules_directory':'/var/lib/app/rules'}}},
        'schema_config':{'configs':[{'from':'2026-01-01','store':'tsdb','object_store':'filesystem','schema':'v13','index':{'prefix':'index_','period':'24h'}}]},
        'compactor':{'working_directory':'/var/lib/app/compactor','retention_enabled':True,'delete_request_store':'filesystem'},
        'limits_config':{'retention_period':'168h','allow_structured_metadata':True,'ingestion_rate_mb':4,'ingestion_burst_size_mb':8},
        'analytics':{'reporting_enabled':False}}
    config('loki',{'loki.yml':yaml.safe_dump(loki)})
    sources={'apiVersion':1,'datasources':[{'name':'Prometheus','uid':'prometheus','type':'prometheus','access':'proxy','url':'http://prometheus:9090','isDefault':True},
        {'name':'Loki','uid':'loki','type':'loki','access':'proxy','url':'http://loki:3100'}]}
    providers={'apiVersion':1,'providers':[{'name':'Zomboid','type':'file','options':{'path':'/etc/grafana/dashboards'}}]}
    dashboards={}
    for title,uid,queries in [
        ('Zomboid — Game','pz-game',[('Server reachable','up{job="zomboid"}'),('Game metrics','{job="zomboid",__name__=~".*(players|memory|heap|uptime).*"}')]),
        ('Zomboid — Host','pz-host',[('Memory','system_memory_usage_bytes'),('Filesystem utilization','system_filesystem_utilization_ratio'),('CPU time','rate(system_cpu_time_seconds_total[5m])')]),
        ('Zomboid — Panel','pz-panel',[('HTTP check','httpcheck_status{check_name="panel-http",http_status_class="2xx"}'),('Collector','up{job="collector"}')]),
        ('Zomboid — Backups','pz-backups',[('Verified backup age','pz_backup_age_seconds'),('Backup monitor','up{job="pz-backup"}'),('Committed backup','pz_backup_committed_present')])]:
        panels=[{'id':i+1,'type':'timeseries','title':label,'gridPos':{'x':0,'y':i*8,'w':24,'h':8},
                 'datasource':{'type':'prometheus','uid':'prometheus'},'targets':[{'refId':'A','expr':expr}]} for i,(label,expr) in enumerate(queries)]
        dashboards[uid+'.json']=json.dumps({'uid':uid,'title':title,'schemaVersion':40,'version':1,'refresh':'30s','time':{'from':'now-1h','to':'now'},'panels':panels},ensure_ascii=False)
    config('grafana-provisioning',{'datasources.yml':yaml.safe_dump(sources),'dashboards.yml':yaml.safe_dump(providers)})
    config('grafana-dashboards',dashboards)
    app('prometheus',9090,65534,'1024Mi','300m',['--config.file=/etc/config/prometheus.yml','--storage.tsdb.path=/var/lib/app','--storage.tsdb.retention.time=15d','--storage.tsdb.retention.size=15GB','--web.enable-remote-write-receiver','--web.external-url=https://prometheus.updspace.com'],[('prometheus','prometheus.yml','/etc/config/prometheus.yml'),('prometheus','rules.yml','/etc/config/rules.yml')],'/-/ready')
    app('alertmanager',9093,65534,'128Mi','100m',['--config.file=/etc/config/alertmanager.yml','--storage.path=/var/lib/app','--cluster.listen-address=','--web.external-url=https://alerts.updspace.com'],[('alertmanager','alertmanager.yml','/etc/config/alertmanager.yml')],'/-/ready')
    app('loki',3100,10001,'1024Mi','300m',['-config.file=/etc/config/loki.yml'],[('loki','loki.yml','/etc/config/loki.yml')],'/ready')
    app('grafana',3000,472,'768Mi','200m',[],[('grafana-provisioning','datasources.yml','/etc/grafana/provisioning/datasources/local.yml'),('grafana-provisioning','dashboards.yml','/etc/grafana/provisioning/dashboards/local.yml')]+[('grafana-dashboards',key,'/etc/grafana/dashboards/'+key) for key in dashboards],'/api/health')
    resources.append({'apiVersion':'networking.k8s.io/v1','kind':'NetworkPolicy','metadata':{'name':'local-prometheus-scrape','namespace':NS},'spec':{'podSelector':{'matchLabels':{'app.kubernetes.io/name':'prometheus'}},'policyTypes':['Egress'],'egress':[
        {'to':[{'namespaceSelector':{'matchLabels':{'kubernetes.io/metadata.name':'zomboid'}},'podSelector':{'matchLabels':{'app.kubernetes.io/name':'zomboid'}}}], 'ports':[{'protocol':'TCP','port':9090}]},
        {'to':[{'ipBlock':{'cidr':'192.168.1.176/32'}}],'ports':[{'protocol':'TCP','port':9109}]}]}})
    resources.append({'apiVersion':'networking.k8s.io/v1','kind':'NetworkPolicy','metadata':{'name':'grafana-lan','namespace':NS},'spec':{'podSelector':{'matchLabels':{'app.kubernetes.io/name':'grafana'}},'policyTypes':['Ingress'],'ingress':[{'from':[{'ipBlock':{'cidr':'192.168.1.0/24'}}],'ports':[{'protocol':'TCP','port':3000}]}]}})
    resources.append({'apiVersion':'networking.k8s.io/v1','kind':'NetworkPolicy','metadata':{'name':'game-from-local-prometheus','namespace':'zomboid'},'spec':{'podSelector':{'matchLabels':{'app.kubernetes.io/name':'zomboid'}},'policyTypes':['Ingress'],'ingress':[{'from':[{'namespaceSelector':{'matchLabels':{'kubernetes.io/metadata.name':NS}},'podSelector':{'matchLabels':{'app.kubernetes.io/name':'prometheus'}}}],'ports':[{'protocol':'TCP','port':9090}]}]}})
    collector=yaml.safe_load((ROOT/'collector-source.yaml').read_text())
    collector['exporters']={'prometheus_remote_write/local':{'endpoint':'http://prometheus:9090/api/v1/write','resource_to_telemetry_conversion':{'enabled':True}},
        'otlp_http/local_logs':{'endpoint':'http://loki:3100/otlp','sending_queue':{'storage':'file_storage','queue_size':1000},'retry_on_failure':{'enabled':True,'max_elapsed_time':'0s'}}}
    for name in ('metrics/game','metrics/backup','metrics/collector'):
        collector['service']['pipelines'].pop(name)
    for name,pipeline in collector['service']['pipelines'].items():
        pipeline['exporters']=['otlp_http/local_logs' if name.startswith('logs') else 'prometheus_remote_write/local']
    collector['processors']['resource/common']['attributes']=[{'key':'cluster','value':'updspace-home','action':'upsert'},{'key':'host.name','value':'updspace-home','action':'upsert'}]
    return resources,collector

if __name__=='__main__':
    resources,collector=build()
    (ROOT/'resources.json').write_text(json.dumps({'apiVersion':'v1','kind':'List','items':resources},indent=2,ensure_ascii=False)+'\n')
    (ROOT/'collector.yaml').write_text(yaml.safe_dump(collector,sort_keys=False))
