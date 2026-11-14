import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
import warnings
from unittest.mock import patch
from unittest.mock import Mock
import urllib.request
import urllib.error
import zipfile

from njust_wifi import __version__
from njust_wifi.updater import UpdateManager, version, extract_verified, asset_url, REPOSITORY


def release(tag='v9.0.0'):
    name=f'NJUSTWiFi-{tag[1:]}-Windows-x64.zip'
    return {'tag_name':tag,'draft':False,'prerelease':False,'assets':[{'name':n,'size':100,'browser_download_url':f'https://github.com/{REPOSITORY}/releases/download/{tag}/{n}'} for n in (name,'SHA256.json')]}


def archive_bytes(extra=None):
    f=io.BytesIO()
    with zipfile.ZipFile(f,'w') as z:
        for n in ('NJUSTWiFi.exe','NJUSTWiFiWindow.exe','NJUSTWiFiBackend.exe','NJUSTWiFi.ico','_internal/frontend/index.html'):
            z.writestr('NJUSTWiFi/'+n,b'fixture')
        if extra:
            entry=zipfile.ZipInfo('fixture');entry.filename=extra
            with warnings.catch_warnings():
                warnings.simplefilter('ignore',UserWarning)
                z.writestr(entry,b'extra')
    return f.getvalue()


class UpdateTests(unittest.TestCase):
    def setUp(self):
        t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup)
        self.root=Path(t.name)
        self.manager=UpdateManager(self.root)

    def test_versions_are_numeric_stable_only(self):
        self.assertGreater(version('v3.10.0'),version('3.9.9'))
        for s in ('latest','v3.2','v3.2.1-beta','3.01.0','../9.0.0'):
            with self.assertRaises(ValueError):version(s)

    def test_release_selects_exact_repo_assets_and_newer_only(self):
        with patch('njust_wifi.updater.read_json',return_value=release()):self.manager.check()
        self.assertEqual(self.manager.status()['state'],'available')
        with patch('njust_wifi.updater.read_json',return_value=release('v'+__version__)):self.manager.check()
        self.assertEqual(self.manager.status()['state'],'current')
        self.assertIsNone(self.manager.release)

    def test_public_manifest_handles_shared_api_rate_limit_without_token(self):
        rows=[{'file':'NJUSTWiFi-9.0.0-Windows-x64.zip','bytes':123,'sha256':'a'*64}]
        error=urllib.error.HTTPError('https://api.github.com/',403,'rate limit',{},io.BytesIO())
        with patch('njust_wifi.updater.read_json',side_effect=[error,rows]) as read:self.manager.check()
        self.assertEqual(self.manager.status()['state'],'available')
        self.assertEqual(self.manager.release['tag'],'v9.0.0')
        self.assertIn('/releases/latest/download/SHA256.json',read.call_args.args[0])

    def test_incomplete_or_redirected_release_is_rejected(self):
        for broken in ({'tag_name':'v9.0.0','assets':[]},release(),release()):
            if broken.get('assets'):
                if broken is not None:broken['assets'][0]['browser_download_url']='https://example.com/payload.zip'
            with patch('njust_wifi.updater.read_json',return_value=broken),self.assertRaises(ValueError):self.manager.check()
        with self.assertRaises(ValueError):asset_url('https://github.com/other/repo/releases/download/v9.0.0/test.zip','v9.0.0','test.zip')

    def test_extracts_and_hashes_required_files(self):
        p=self.root/'update.zip';p.write_bytes(archive_bytes())
        stage=self.root/'stage';manifest=extract_verified(p,stage)
        self.assertEqual(len(manifest),5)
        self.assertEqual(manifest['NJUSTWiFi.exe'],hashlib.sha256(b'fixture').hexdigest())

    def test_rejects_paths_duplicates_links_secrets_and_missing_files(self):
        for n in ('NJUSTWiFi/../../escape','NJUSTWiFi/C:evil','NJUSTWiFi/credentials.json','NJUSTWiFi/a\\evil','other/evil','NJUSTWiFi/NJUSTWiFi.exe','NJUSTWiFi/space. '):
            p=self.root/'update.zip';p.write_bytes(archive_bytes(n))
            with self.assertRaises(ValueError):extract_verified(p,self.root/'stage')
            self.assertFalse((self.root/'stage').exists())
        f=io.BytesIO()
        with zipfile.ZipFile(f,'w') as z:
            link=zipfile.ZipInfo('NJUSTWiFi/link');link.external_attr=(0o120777<<16);z.writestr(link,'outside')
        p.write_bytes(f.getvalue())
        with self.assertRaises(ValueError):extract_verified(p,self.root/'stage')

    def test_download_size_and_hash_fail_before_install(self):
        self.manager.install_root=self.root/'installed';self.manager.install_root.mkdir()
        with patch('njust_wifi.updater.read_json',return_value=release()):self.manager.check()
        selected=self.manager.release
        checksum=[{'file':selected['name'],'bytes':100,'sha256':'0'*64}]
        with patch('njust_wifi.updater.read_json',return_value=checksum),patch('njust_wifi.updater.open_release',return_value=io.BytesIO(b'not-valid')),self.assertRaises(ValueError):self.manager.download()
        self.assertIsNone(self.manager.stage)
        self.assertEqual(list(self.manager.install_root.iterdir()),[])

    def test_valid_download_stages_but_does_not_replace_install(self):
        original=self.root/'installed';original.mkdir();(original/'marker').write_text('old')
        self.manager.install_root=original
        payload=archive_bytes()
        rel=release();rel['assets'][0]['size']=len(payload)
        with patch('njust_wifi.updater.read_json',return_value=rel):self.manager.check()
        checksum=[{'file':self.manager.release['name'],'bytes':len(payload),'sha256':hashlib.sha256(payload).hexdigest()}]
        with patch('njust_wifi.updater.read_json',return_value=checksum),patch('njust_wifi.updater.open_release',return_value=io.BytesIO(payload)):self.manager.download()
        self.assertEqual(self.manager.status()['state'],'ready')
        self.assertEqual((original/'marker').read_text(),'old')
        self.assertEqual(len(self.manager.manifest),5)

    def test_source_and_demo_do_not_install_or_spawn_helpers(self):
        self.manager.release=release()
        with self.assertRaises(ValueError):self.manager.download()
        with patch('njust_wifi.updater.subprocess.run') as run,self.assertRaises(ValueError):self.manager.install()
        run.assert_not_called()

    def test_no_parallel_download_or_install_before_ready(self):
        self.manager.set(state='downloading')
        with self.assertRaises(ValueError):self.manager.start('check')
        with self.assertRaises(ValueError):self.manager.install()


class UpdateApiTests(unittest.TestCase):
    def test_update_routes_require_session_and_install_respects_busy_state(self):
        from njust_wifi.service import Service
        from njust_wifi.storage import Store
        from njust_wifi.server import Server
        with tempfile.TemporaryDirectory() as temp:
            service=Service(Store(Path(temp)),demo=True)
            service.updater=Mock()
            service.updater.status.return_value={'state':'idle'}
            server=Server(service,Path(temp),0)
            worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
            opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
            origin=server.origin
            try:
                with self.assertRaises(urllib.error.HTTPError) as err:opener.open(origin+'/api/update')
                self.assertEqual(err.exception.code,403)
                err.exception.close()
                headers={'X-NJUST-Token':server.token,'Content-Type':'application/json'}
                with opener.open(urllib.request.Request(origin+'/api/update/check',data=b'{}',headers=headers)) as r:self.assertEqual(r.status,200)
                service.updater.start.assert_called_once_with('check')
                service.busy=True
                with self.assertRaises(urllib.error.HTTPError) as err:opener.open(urllib.request.Request(origin+'/api/update/install',data=b'{}',headers=headers))
                self.assertEqual(err.exception.code,400)
                err.exception.close()
                service.updater.install.assert_not_called()
                service.busy=False
                with opener.open(urllib.request.Request(origin+'/api/update/install',data=b'{}',headers=headers)) as r:self.assertEqual(r.status,200)
                service.updater.install.assert_called_once_with(running=False)
            finally:server.shutdown();server.server_close();service.close();worker.join()


if __name__=='__main__':unittest.main()
