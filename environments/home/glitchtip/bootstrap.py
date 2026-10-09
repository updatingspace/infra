#!/usr/bin/env python3
"""VM operator bootstrap; preserves existing passwords and refuses foreign ownership."""
import argparse
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess

PRIVATE = Path('/opt/updspace-infra/private/glitchtip-credentials.json')
KUBECTL = ['k3s', 'kubectl']


def sql(query):
    result = subprocess.run(KUBECTL + ['-n', 'updspace-data', 'exec', '-i', 'postgres-0', '--',
        'psql', '-X', '-qAt', '-v', 'ON_ERROR_STOP=1', '-U', 'postgres', '-d', 'postgres'],
        input=query, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError('PostgreSQL bootstrap failed; SQL and stderr withheld to protect credentials')
    return result.stdout.strip()


def state():
    return json.loads(sql("""SELECT json_build_object(
      'role', (SELECT json_build_object('super',rolsuper,'createdb',rolcreatedb,
        'createrole',rolcreaterole,'replication',rolreplication,'bypassrls',rolbypassrls,
        'login',rolcanlogin,'connections',rolconnlimit) FROM pg_roles WHERE rolname='glitchtip'),
      'owner', (SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname='glitchtip'));
    """))


def validate_state(current):
    if current['owner'] not in (None, 'glitchtip'):
        raise ValueError('Refusing database with a different owner')
    role = current['role']
    if role is not None and role != dict(super=False, createdb=False, createrole=False,
            replication=False, bypassrls=False, login=True, connections=12):
        raise ValueError('Existing role privileges differ; inspect before applying')


def credentials(current, path=PRIVATE):
    if not path.exists():
        if current['role'] or current['owner']:
            raise ValueError('Existing database/role without private credentials; restore them, do not rotate')
        path.parent.mkdir(parents=True, exist_ok=True)
        value = {'database_password': secrets.token_urlsafe(32), 'secret_key': secrets.token_urlsafe(48),
                 'admin_email': 'admin@updspace.com', 'admin_password': secrets.token_urlsafe(24)}
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as file:
            json.dump(value, file)
    mode = path.lstat()
    if not stat.S_ISREG(mode.st_mode) or mode.st_uid != os.geteuid() or stat.S_IMODE(mode.st_mode) & 0o077:
        raise ValueError('Credentials must be an operator-owned private regular file')
    value = json.loads(path.read_text())
    for key in ('database_password', 'secret_key', 'admin_password'):
        if not re.fullmatch(r'[A-Za-z0-9_-]{24,}', value.get(key, '')):
            raise ValueError('Invalid private credential format')
    return value


def database():
    current = state()
    validate_state(current)
    private = credentials(current)
    if current['role'] is None:
        sql("CREATE ROLE glitchtip LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION "
            "NOBYPASSRLS CONNECTION LIMIT 12 PASSWORD '" + private['database_password'] + "';")
    if current['owner'] is None:
        sql('CREATE DATABASE glitchtip OWNER glitchtip;')
    sql('REVOKE ALL ON DATABASE glitchtip FROM PUBLIC; GRANT CONNECT,TEMPORARY ON DATABASE glitchtip TO glitchtip;')
    validate_state(state())
    secret = {'apiVersion': 'v1', 'kind': 'Secret',
              'metadata': {'name': 'glitchtip-runtime', 'namespace': 'glitchtip'},
              'type': 'Opaque', 'stringData': {
                  'DATABASE_URL': 'postgres://glitchtip:' + private['database_password'] +
                      '@postgres.updspace-data.svc.cluster.local:5432/glitchtip',
                  'SECRET_KEY': private['secret_key']}}
    subprocess.run(KUBECTL + ['apply', '-f', '-'], input=json.dumps(secret), text=True, check=True)
    print('Isolated database and runtime Secret ready; existing passwords preserved')


ACCOUNT = '''
import json,sys
from django.db import transaction
from django.contrib.auth import get_user_model
from allauth.account.models import EmailAddress
from apps.organizations_ext.models import Organization,OrganizationUser,OrganizationOwner
from apps.organizations_ext.constants import OrganizationUserRole
from apps.projects.models import Project
c=json.load(sys.stdin)
with transaction.atomic():
    user=get_user_model().objects.filter(email=c['admin_email']).first()
    if user is None:
        user=get_user_model().objects.create_superuser(c['admin_email'],c['admin_password'])
    assert user.is_staff and user.is_superuser and user.is_active
    EmailAddress.objects.get_or_create(user=user,email=user.email,defaults={'primary':True,'verified':True})
    org,_=Organization.objects.get_or_create(slug='updspace',defaults={'name':'UpdatingSpace LLC'})
    member,_=OrganizationUser.objects.get_or_create(organization=org,user=user,defaults={'role':OrganizationUserRole.OWNER})
    assert member.role==OrganizationUserRole.OWNER
    owner,_=OrganizationOwner.objects.get_or_create(organization=org,defaults={'organization_user':member})
    assert owner.organization_user_id==member.id
    project,_=Project.objects.get_or_create(organization=org,slug='infra-smoke',defaults={'name':'Infra Smoke','platform':'other'})
    key=project.projectkey_set.filter(is_active=True).first()
    assert key is not None
    print(json.dumps({'project_id':project.id,'dsn':key.get_dsn()}))
'''


def account():
    private = credentials(state())
    result = subprocess.run(KUBECTL + ['-n', 'glitchtip', 'exec', '-i', 'deployment/glitchtip', '--',
        'python', 'manage.py', 'shell', '-c', ACCOUNT], input=json.dumps(private), text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError('Account bootstrap failed; output withheld to protect credentials')
    private.update(json.loads(result.stdout.strip().splitlines()[-1]))
    PRIVATE.write_text(json.dumps(private))
    print('Admin and UpdSpace/infra-smoke ready; credentials and DSN stored only in private operator file')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', choices=['database', 'account'])
    args = parser.parse_args()
    if args.apply == 'database':
        database()
    elif args.apply == 'account':
        account()
    else:
        current = state()
        validate_state(current)
        print(json.dumps(current))
