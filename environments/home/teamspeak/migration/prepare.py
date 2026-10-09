import base64,hashlib,json,pathlib,subprocess,datetime,os
manifest=(pathlib.Path(__file__).resolve().parents[1] / 'mariadb.yaml').read_text()
name='teamspeak-docker-db-1'
x=json.loads(subprocess.check_output(['docker','inspect',name]))[0]
assert x['State']['Running'] and x['Image']=='sha256:6722945a6940fd6c3e394cb4791057f7210b0ead90eee0f85094cd24e2ca412d'
env=dict(v.split('=',1) for v in x['Config']['Env'])
assert env['MYSQL_DATABASE']=='teamspeak'
source=pathlib.Path(next(m['Source'] for m in x['Mounts'] if m['Destination']=='/var/lib/mysql'))
assert str(source).startswith('/var/lib/docker/volumes/') and source.is_dir()
config='[client]\nuser=root\npassword="'+env['MYSQL_ROOT_PASSWORD'].replace('\\','\\\\').replace('"','\\"')+'"\n'
script='umask 077; f=$(mktemp); cat > "$f"; mariadb --defaults-extra-file="$f" --batch --skip-column-names -e "$1"; rc=$?; rm -f "$f"; exit "$rc"'
def query(sql):
 r=subprocess.run(['docker','exec','-i',name,'sh','-c',script,'sh',sql],input=config,text=True,capture_output=True)
 assert r.returncode==0,'Database check failed (details suppressed)'
 return r.stdout
assert query('SELECT COUNT(*) FROM information_schema.PROCESSLIST WHERE ID<>CONNECTION_ID() AND USER<>\'system user\';').strip()=='0','Active database clients: refuse cutover'
tables=query("SELECT table_name FROM information_schema.tables WHERE table_schema='teamspeak' ORDER BY table_name;").splitlines()
assert len(tables)==28 and all(t.replace('_','').isalnum() for t in tables)
sql=';'.join('CHECKSUM TABLE `teamspeak`.`'+t+'` EXTENDED' for t in tables)+';'
checks=query(sql)
assert all(line.split('\t')[-1]!='NULL' for line in checks.splitlines())
base=pathlib.Path('/opt/updspace-infra/private/teamspeak-migration');assert not base.exists(),'Existing migration: inspect before repeating'
base.mkdir(parents=True,mode=0o700)
def private(path,data):
 fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
 with os.fdopen(fd,'w') as f:f.write(data)
private(base/'client.cnf',config)
private(base/'checksums-before.txt',checks)
private(base/'migration.json',json.dumps({'source':str(source),'image':x['Image'],'restart_policy':x['HostConfig']['RestartPolicy'],'table_names':tables,'checksum_sql':sql,'at':datetime.datetime.now(datetime.timezone.utc).isoformat()},indent=2))
private(base/'mariadb.yaml',manifest)
# Preflight only the namespace first, then dry-run the exact new manifest.
namespace={'apiVersion':'v1','kind':'Namespace','metadata':{'name':'teamspeak','labels':{'pod-security.kubernetes.io/enforce':'restricted','pod-security.kubernetes.io/enforce-version':'v1.36'}}}
subprocess.run(['k3s','kubectl','apply','-f','-'],input=json.dumps(namespace),text=True,check=True)
subprocess.run(['k3s','kubectl','apply','--dry-run=server','-f',str(base/'mariadb.yaml')],check=True)
secret={'apiVersion':'v1','kind':'Secret','metadata':{'name':'mariadb-existing','namespace':'teamspeak'},'type':'Opaque','data':{'root-password':base64.b64encode(env['MYSQL_ROOT_PASSWORD'].encode()).decode()}}
subprocess.run(['k3s','kubectl','create','-f','-'],input=json.dumps(secret),text=True,check=True,stdout=subprocess.DEVNULL)
# Import the exact locally installed bytes without replacing or upgrading the image.
archive=base/'mariadb-image.tar'
subprocess.run(['docker','save','-o',str(archive),'mariadb@sha256:a9547599cd87d7242435aea6fda22a9d83e2c06d16c658ef70d2868b3d3f6a80'],check=True)
archive.chmod(0o600)
subprocess.run(['k3s','ctr','images','import',str(archive)],check=True,stdout=subprocess.DEVNULL)
print(json.dumps({'prepared':True,'tables':len(tables),'source_still_running':True,'private_backup_directory':str(base)}))
