#!/usr/bin/env python3
"""Idempotent declared organization OIDC provider; no personal account mutations."""
import argparse
import json
import os
from pathlib import Path
import stat
import subprocess

ROOT = Path(__file__).resolve().parent
APPLY = '''
import json,sys
from django.db import transaction
from allauth.socialaccount.models import SocialApp
from apps.organizations_ext.models import Organization,OrganizationSocialApp
c=json.load(sys.stdin)
with transaction.atomic():
    org=Organization.objects.select_for_update().get(slug=c['organization'])
    assert org.organization_users.exists(), 'Refuse empty organization bootstrap'
    assert org.name in ('UpdSpace',c['organization_name']), 'Unexpected organization name'
    if c['apply'] and org.name!=c['organization_name']:
        org.name=c['organization_name'];org.save(update_fields=['name'])
    apps=list(SocialApp.objects.select_for_update().filter(provider_id=c['provider_id']))
    assert len(apps)<=1, 'Ambiguous social provider'
    app=apps[0] if apps else None
    expected={k:c[k] for k in ('provider','provider_id','name','client_id','settings')}
    if app:
        assert all(getattr(app,k)==v for k,v in expected.items()), 'Existing provider differs'
        assert app.secret==c['secret'], 'Existing provider key differs; no rotation attempted'
        link=OrganizationSocialApp.objects.get(social_app=app)
        assert link.organization_id==org.id and link.is_public==c['is_public']
    elif c['apply']:
        app=SocialApp.objects.create(**expected,secret=c['secret'])
        OrganizationSocialApp.objects.create(organization=org,social_app=app,is_public=c['is_public'])
    print(json.dumps({'provider':c['provider_id'],'configured':app is not None,'organization':org.slug,'organizationName':org.name,'usersChanged':False}))
'''


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--apply',action='store_true');args=parser.parse_args()
    if os.geteuid()!=0:raise SystemExit('Run as root on the VM')
    config=json.loads((ROOT/'oidc.json').read_text())
    assert config['provider']=='openid_connect' and config['provider_id']=='updspace'
    assert config['client_id']=='observability' and config['organization']=='updspace'
    assert config['settings']['server_url']=='https://id.updspace.com/.well-known/openid-configuration'
    fd=os.open('/opt/updspace-infra/private/observability-oidc.json',os.O_RDONLY|os.O_NOFOLLOW)
    with os.fdopen(fd) as source:
        info=os.fstat(source.fileno());assert stat.S_ISREG(info.st_mode) and info.st_uid==0 and not info.st_mode&0o077
        config['secret']=json.load(source)['client_secret']
    config['apply']=args.apply
    result=subprocess.run(['k3s','kubectl','-n','glitchtip','exec','-i','deployment/glitchtip','--','python','manage.py','shell','-c',APPLY],input=json.dumps(config),text=True,capture_output=True)
    if result.returncode:raise RuntimeError('Provider check/apply failed; no secret output. Inspect configuration before retrying')
    result=json.loads(result.stdout.strip().splitlines()[-1]);print(json.dumps(result))


if __name__=='__main__':main()
