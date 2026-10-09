import json,pathlib,subprocess
base=pathlib.Path('/opt/updspace-infra/private/teamspeak-migration')
state=json.loads((base/'migration.json').read_text())
config=(base/'client.cnf').read_text()
script='umask 077; f=$(mktemp); cat > "$f"; mariadb --defaults-extra-file="$f" --batch --skip-column-names -e "$1"; rc=$?; rm -f "$f"; exit "$rc"'
def query(sql):
 r=subprocess.run(['k3s','kubectl','-n','teamspeak','exec','-i','mariadb-0','--','sh','-c',script,'sh',sql],input=config,text=True,capture_output=True)
 assert r.returncode==0,'Database acceptance failed (details suppressed)'
 return r.stdout
version=query('SELECT VERSION();').strip()
assert version=='11.6.2-MariaDB-ubu2404'
assert query(state['checksum_sql'])==(base/'checksums-before.txt').read_text(),'Database table checksums differ'
source=json.loads(subprocess.check_output(['docker','inspect','teamspeak-docker-db-1']))[0]
assert not source['State']['Running'] and source['HostConfig']['RestartPolicy']['Name']=='no'
pod=json.loads(subprocess.check_output(['k3s','kubectl','-n','teamspeak','get','pod','mariadb-0','-o','json']))
assert pod['status']['containerStatuses'][0]['ready']
proof={'version':version,'tables':len(state['table_names']),'table_checksums_equal':True,'original_password_works':True,'source_stopped_and_retained':True,'pod_ready':True,'resources':pod['spec']['containers'][0]['resources'],'image':pod['spec']['containers'][0]['image'],'docker_applications_running':subprocess.check_output(['docker','ps','--format','{{.Names}}'],text=True).splitlines()}
(base/'acceptance.json').write_text(json.dumps(proof,indent=2)+'\n')
print(json.dumps(proof,indent=2))
