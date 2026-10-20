#!/usr/bin/env python3
"""Build pinned, offline-importable images. Does not deploy or access runtime secrets."""
import hashlib,json,os,shutil,subprocess,urllib.request,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parent
build=ROOT/'build';build.mkdir(exist_ok=True)
release=json.loads((ROOT/'xray-release.json').read_text())
archive=build/'xray.zip'
if not archive.exists():urllib.request.urlretrieve(release['url'],archive)
actual='sha256:'+hashlib.sha256(archive.read_bytes()).hexdigest()
assert actual==release['digest'],'Xray release checksum mismatch'
with zipfile.ZipFile(archive) as z:
    for name in ['xray','LICENSE']:(build/name).write_bytes(z.read(name))
(build/'xray').chmod(0o755)
shutil.copyfile('/etc/ssl/certs/ca-certificates.crt',build/'ca-certificates.crt')
env=dict(os.environ,CGO_ENABLED='1')
subprocess.run(['go','build','-buildvcs=false','-tags','netgo,osusergo,sqlite_omit_load_extension','-ldflags','-s -w -linkmode external -extldflags -static','-o',str(build/'notify-bridge'),'.'],cwd=ROOT/'app',env=env,check=True)
images={kind:'docker.io/updspace-local/'+kind+':'+hashlib.sha256((build/binary).read_bytes()).hexdigest()[:16] for kind,binary in [('bridge','notify-bridge'),('xray','xray')]}
for kind in ['bridge','xray']:subprocess.run(['docker','build','-f','Dockerfile.'+kind,'-t',images[kind],'.'],cwd=ROOT,check=True)
subprocess.run(['docker','save','-o',str(build/'images.tar'),*images.values()],check=True)
(ROOT/'images.json').write_text(json.dumps(images,indent=2)+'\n')
print('Images ready in build/images.tar; review images.json before deployment')
