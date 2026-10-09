import gzip
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

SPEC=importlib.util.spec_from_file_location('oci_import',Path(__file__).with_name('oci-import.py'))
oci=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(oci)

class ImportTests(unittest.TestCase):
    def fixture(self, directory, *, corrupt_diff=False, corrupt_layer=False, compress=True):
        data=io.BytesIO()
        with tarfile.open(fileobj=data,mode='w') as tar:
            content=b'verified image bytes\n';info=tarfile.TarInfo('proof.txt');info.size=len(content);tar.addfile(info,io.BytesIO(content))
        layer=data.getvalue();packed=gzip.compress(layer) if compress else layer
        members={}
        def blob(raw,kind):
            digest='sha256:'+hashlib.sha256(raw).hexdigest();members['blobs/sha256/'+digest[7:]]=raw
            return {'digest':digest,'size':len(raw),'mediaType':kind}
        descriptor=blob(packed,'application/vnd.oci.image.layer.v1.tar'+('+gzip' if compress else ''))
        config_raw=json.dumps({'os':'linux','architecture':'amd64','rootfs':{'type':'layers','diff_ids':['sha256:'+('0'*64 if corrupt_diff else hashlib.sha256(layer).hexdigest())]},'config':{}}).encode()
        config=blob(config_raw,'application/vnd.oci.image.config.v1+json')
        manifest=blob(json.dumps({'schemaVersion':2,'config':config,'layers':[descriptor]}).encode(),'application/vnd.oci.image.manifest.v1+json')
        members['index.json']=json.dumps({'schemaVersion':2,'manifests':[manifest]}).encode()
        members['oci-layout']=b'{"imageLayoutVersion":"1.0.0"}'
        if corrupt_layer:members['blobs/sha256/'+descriptor['digest'][7:]]=b'x'*len(packed)
        path=Path(directory)/'image.tar'
        with tarfile.open(path,'w') as tar:
            for name,raw in members.items():
                info=tarfile.TarInfo(name);info.size=len(raw);tar.addfile(info,io.BytesIO(raw))
        return path,config['digest'],config_raw,layer

    def test_gzip_and_plain_layers_preserve_exact_config_and_rootfs_bytes(self):
        for compress in (True,False):
            with self.subTest(compress=compress), tempfile.TemporaryDirectory() as directory:
                path,digest,config,layer=self.fixture(directory,compress=compress);output=io.BytesIO()
                result=oci.stream_docker_archive(str(path),digest,output)
                self.assertEqual(result['layers'],1)
                with tarfile.open(fileobj=io.BytesIO(output.getvalue())) as tar:
                    manifest=json.load(tar.extractfile('manifest.json'))[0]
                    self.assertEqual(manifest['RepoTags'],[])
                    self.assertEqual(tar.extractfile(manifest['Config']).read(),config)
                    self.assertEqual(tar.extractfile(manifest['Layers'][0]).read(),layer)
                    self.assertEqual('sha256:'+hashlib.sha256(config).hexdigest(),digest)

    def test_corrupt_diff_id_fails_before_writing_docker_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            path,digest,_,_=self.fixture(directory,corrupt_diff=True);output=io.BytesIO()
            with self.assertRaisesRegex(oci.ImportError,'mismatch'):oci.stream_docker_archive(str(path),digest,output)
            self.assertEqual(output.getvalue(),b'')

    def test_wrong_config_never_imports_another_image(self):
        with tempfile.TemporaryDirectory() as directory:
            path,_,_,_=self.fixture(directory)
            with self.assertRaisesRegex(oci.ImportError,'missing_or_ambiguous'):oci.stream_docker_archive(str(path),'sha256:'+'0'*64,io.BytesIO())

    def test_bad_compressed_layer_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path,digest,_,_=self.fixture(directory,corrupt_layer=True)
            with self.assertRaises((oci.ImportError,gzip.BadGzipFile)):oci.stream_docker_archive(str(path),digest,io.BytesIO())

if __name__=='__main__':unittest.main()
