#!/usr/bin/env python3
"""Review or reconcile only the declared DNS records; never delete records."""
import argparse, json, os, urllib.error, urllib.parse, urllib.request
from pathlib import Path

def changes(desired,current):
 result=[]
 for record in desired:
  assert record['type']=='CNAME' and record['name'].endswith('.updspace.com')
  assert record['name']!='*.updspace.com' and record['ttl'] in (1,60) and isinstance(record['proxied'],bool)
  matches=[x for x in current if x['name']==record['name']]
  assert len(matches)<=1, 'Ambiguous DNS ownership: '+record['name']
  if not matches:result.append(('POST',None,record))
  elif any(matches[0].get(k)!=v for k,v in record.items()):
   assert matches[0]['type']==record['type'],'Record type change requires explicit migration'
   result.append(('PATCH',matches[0]['id'],record))
 return result

def main():
 parser=argparse.ArgumentParser();parser.add_argument('--apply',action='store_true');args=parser.parse_args()
 manifest=json.loads((Path(__file__).resolve().parents[1]/'environments/home/dns/records.json').read_text())
 token=os.environ['CLOUDFLARE_API_TOKEN']
 path='https://api.cloudflare.com/client/v4/zones/'+manifest['zone_id']+'/dns_records'
 def request(method,url,data=None):
  req=urllib.request.Request(url,method=method,headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'},data=None if data is None else json.dumps(data).encode())
  try:
   with urllib.request.urlopen(req,timeout=30) as response:body=json.load(response)
  except urllib.error.HTTPError as error:raise RuntimeError(f'Cloudflare HTTP {error.code}') from None
  if not body.get('success'):raise RuntimeError('Cloudflare operation failed')
  return body['result']
 current=[]
 for record in manifest['records']:
  current.extend(request('GET',path+'?'+urllib.parse.urlencode({'name':record['name']})))
 planned=changes(manifest['records'],current)
 for method,record_id,record in planned:
  print(method+' '+record['name'])
  if args.apply:request(method,path+('/'+record_id if record_id else ''),record)
 if not planned:print('DNS matches the versioned manifest')
if __name__=='__main__':main()
