#!/usr/bin/env python3
"""Read-only acceptance for public HTTPS and the temporary edge authentication."""
import argparse,base64,json,subprocess,urllib.parse
from pathlib import Path
parser=argparse.ArgumentParser();parser.add_argument('--origin-ip');args=parser.parse_args()
credentials=json.loads(Path('/opt/updspace-infra/private/monitoring-credentials.json').read_text())
auth='Basic '+base64.b64encode((credentials['username']+':'+credentials['password']).encode()).decode()
def get(url,authorization=None):
 config='' if authorization is None else 'header = "Authorization: '+authorization+'"\n'
 extra=[] if not args.origin_ip else ['--resolve',urllib.parse.urlsplit(url).hostname+':443:'+args.origin_ip]
 result=subprocess.run(['curl','--compressed','--silent','--show-error','--max-time','20','--config','-','--write-out','\n%{http_code}',url]+extra,input=config.encode(),capture_output=True,check=True)
 body,status=result.stdout.rsplit(b'\n',1)
 return int(status),body
for domain,path in [('grafana','/login'),('prometheus','/api/v1/query?query=up'),('alerts','/api/v2/status')]:
 url='https://'+domain+'.updspace.com'+path
 status,_=get(url);assert status==401,(domain,'anonymous',status)
 wrong,_=get(url,'Basic bW9uaXRvcmluZzppbnZhbGlk');assert wrong==401,(domain,'wrong password',wrong)
 status,body=get(url,auth);assert status==200,(domain,'authenticated',status)
 if domain=='prometheus':assert json.loads(body)['status']=='success'
 if domain=='alerts':assert 'versionInfo' in json.loads(body)
 print(json.dumps({'host':domain+'.updspace.com','anonymous':401,'wrongPassword':401,'authenticated':200,'tlsVerified':True,'originOverride':args.origin_ip}))
status,_=get('https://grafana.updspace.com/api/user',auth)
assert status==401,('Grafana own login unexpectedly bypassed',status)
for domain,path in [('status','/api/status-page/updspace'),('pz-admin','/api/health')]:
 status,_=get('https://'+domain+'.updspace.com'+path);assert status==200,(domain,status)
 print(json.dumps({'existingHost':domain+'.updspace.com','http':status,'tlsVerified':True,'originOverride':args.origin_ip}))
