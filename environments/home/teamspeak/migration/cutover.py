import hashlib,json,pathlib,subprocess,os
os.umask(0o077)
base=pathlib.Path('/opt/updspace-infra/private/teamspeak-migration')
state=json.loads((base/'migration.json').read_text());src=pathlib.Path(state['source']);dst=pathlib.Path('/srv/teamspeak/mariadb')
assert not dst.exists() and not (base/'cold-backup.tar').exists(),'Migration has already progressed; inspect rather than repeat'
assert json.loads(subprocess.check_output(['docker','inspect','teamspeak-docker-db-1']))[0]['State']['Running']
subprocess.run(['docker','stop','--time','120','teamspeak-docker-db-1'],check=True,stdout=subprocess.DEVNULL)
assert not json.loads(subprocess.check_output(['docker','inspect','teamspeak-docker-db-1']))[0]['State']['Running']
subprocess.run(['docker','update','--restart=no','teamspeak-docker-db-1'],check=True,stdout=subprocess.DEVNULL)
archive=base/'cold-backup.tar'
subprocess.run(['tar','--sparse','--numeric-owner','-cpf',str(archive),'-C',str(src),'.'],check=True)
dst.mkdir(parents=True,mode=0o700)
subprocess.run(['tar','--sparse','--numeric-owner','-xpf',str(archive),'-C',str(dst)],check=True)
def hashes(root):
 result={}
 for p in sorted(root.rglob('*')):
  assert not p.is_symlink(),'Unexpected source symlink'
  if p.is_file():
   with p.open('rb') as f:result[str(p.relative_to(root))]=hashlib.file_digest(f,'sha256').hexdigest()
 return result
source_hashes=hashes(src);assert source_hashes==hashes(dst),'Cold restore hashes differ'
with archive.open('rb') as f:archive_hash=hashlib.file_digest(f,'sha256').hexdigest()
(base/'restore-proof.json').write_text(json.dumps({'files':len(source_hashes),'sha256':archive_hash,'file_sha256':source_hashes,'cold_restore_equal':True},indent=2)+'\n')
subprocess.run(['k3s','kubectl','apply','-f',str(base/'mariadb.yaml')],check=True)
print(json.dumps({'cold_restore_equal':True,'files':len(source_hashes),'source_retained':str(src),'target':str(dst)}))
