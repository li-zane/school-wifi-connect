import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from njust_wifi.proxy import Controller, ControllerError, ProxyProtection, apply_rules, make_script, strip_script
from njust_wifi.probes import ApplicationGuards


class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        core = self.root / 'core'
        (core/'profiles').mkdir(parents=True)
        self.original = 'function main(config) { config.rules = ["DOMAIN,existing.test,REJECT"].concat(config.rules || []); return config; }\n'
        (core/'profiles/Script.js').write_text(self.original)
        (core/'verge.yaml').write_text('enable_system_proxy: true\nenable_tun_mode: true\nother: unchanged\n')
        self.runtime = 'mode: rule\nmixed-port: 7897\ntun:\n  enable: true\nrules:\n  - DOMAIN,existing.test,REJECT\n  - MATCH,DIRECT\n'
        self.controller = Mock(root=core)
        self.controller.runtime.return_value = self.runtime
        self.config = {'mode':'rule','mixed-port':7897,'tun':{'enable':True}}
        self.calls = []
        def request(method, path, payload=None):
            self.calls.append((method,path,payload))
            if path == '/configs':
                return self.config.copy() if method == 'GET' else {}
            if path == '/proxies':
                return {'proxies':{'Sample':{'type':'Selector'},'DIRECT':{'type':'Direct'},'GLOBAL':{'type':'Selector'}}}
            return {'delay':12}
        self.controller.request.side_effect = request
        self.proxy = ProxyProtection(self.root, controller=self.controller)
        self.exe = self.root / 'sample.exe'
        self.exe.write_bytes(b'fixture')
        self.identifier = ApplicationGuards.identifier(self.exe)
        self.registry = {'ProxyEnable':1, 'ProxyServer':'127.0.0.1:7897'}
        def registry(values=None):
            if values:
                self.registry.update(values)
            return self.registry.copy()
        self.proxy.system_proxy = Mock(side_effect=registry)

    def test_policy_apply_preserves_custom_rules_and_original_script(self):
        self.proxy.configure([self.exe], identifier=self.identifier, mode='direct')
        payload = next(call[2]['payload'] for call in self.calls if call[0] == 'PUT')
        self.assertIn('DOMAIN,existing.test,REJECT', payload)
        self.assertLess(payload.index('PROCESS-PATH'),payload.index('DOMAIN,existing.test'))
        self.assertEqual((self.root/'proxy-script-original.js').read_text(), self.original)
        self.assertIn('schoolWifiOriginalMain.apply', (self.controller.root/'profiles/Script.js').read_text())
        reopened = ProxyProtection(self.root, controller=self.controller)
        self.assertEqual(reopened.policies[self.identifier], 'direct')
        self.assertNotIn('secret', self.proxy.path.read_text())

    def test_group_validation_and_proxy_policy(self):
        with self.assertRaises(ValueError):
            self.proxy.configure([self.exe], identifier=self.identifier, mode='proxy')
        self.proxy.configure([self.exe],group='Sample')
        self.proxy.configure([self.exe],identifier=self.identifier,mode='proxy')
        self.assertTrue(self.proxy.rules([self.exe])[0].endswith(',Sample'))
        with self.assertRaises(ValueError):
            self.proxy.configure([self.exe],group='missing')

    def test_failed_core_validation_keeps_script_and_saved_policy(self):
        before = (self.controller.root/'profiles/Script.js').read_bytes()
        self.controller.request.side_effect = OSError('fixture')
        with self.assertRaises(ValueError):
            self.proxy.configure([self.exe],identifier=self.identifier,mode='direct')
        self.assertEqual((self.controller.root/'profiles/Script.js').read_bytes(),before)
        self.assertFalse(self.proxy.path.exists())
        self.assertEqual(self.proxy.policies,{})

    def test_global_mode_rejects_per_process_policy_without_switching_mode(self):
        self.config['mode'] = 'global'
        with self.assertRaises(ValueError):
            self.proxy.configure([self.exe],identifier=self.identifier,mode='direct')
        self.assertFalse(any(call[0] in ('PUT','PATCH') for call in self.calls))

    def test_removed_program_loses_its_managed_rule(self):
        self.proxy.configure([self.exe],identifier=self.identifier,mode='direct')
        self.proxy.remove([self.exe],{self.identifier})
        self.assertEqual(self.proxy.policies,{})
        self.assertNotIn('PROCESS-PATH',self.calls[-1][2]['payload'])
        self.assertNotIn('SCHOOL-WIFI', (self.controller.root/'profiles/Script.js').read_text())

    def test_yaml_transform_handles_empty_and_quoted_block_lists(self):
        old = r'PROCESS-PATH,C:\Fixture\sample.exe,DIRECT'
        text = 'rules:\n  - '+json.dumps(old)+'\n  - MATCH,DIRECT\ndns:\n  enable: true\n'
        result = apply_rules(text,[],[old])
        self.assertNotIn('PROCESS-PATH',result)
        self.assertIn('MATCH,DIRECT',result)
        self.assertIn('dns:\n  enable: true',result)
        self.assertIn('PROCESS-PATH',apply_rules('rules: []\n',[old]))
        with self.assertRaises(ValueError):
            apply_rules('rules: [MATCH,DIRECT]\n',[old])

    def test_script_reconfiguration_has_one_managed_block(self):
        first = make_script(self.original,['DOMAIN,a.test,DIRECT'])
        second = make_script(first,['DOMAIN,b.test,DIRECT'])
        self.assertEqual(second.count('// BEGIN SCHOOL-WIFI-CONNECT'),1)
        self.assertNotIn('a.test',second)
        self.assertIn('b.test',second)
        self.assertEqual(strip_script(second).strip(),self.original.strip())

    def test_indentationless_verge_rule_list_keeps_valid_sequence_indentation(self):
        source = 'mode: rule\nrules:\n- PROCESS-NAME,existing.exe,DIRECT\n- MATCH,Sample\n'
        result = apply_rules(source, ['PROCESS-PATH,C:\\Sample\\app.exe,Sample'])
        self.assertIn('rules:\n- "PROCESS-PATH,',result)
        self.assertIn('\n- PROCESS-NAME,existing.exe,DIRECT\n- MATCH,Sample\n',result)
        again = apply_rules(result,[],['PROCESS-PATH,C:\\Sample\\app.exe,Sample'])
        self.assertNotIn('PROCESS-PATH',again)
        self.assertIn('rules:\n- PROCESS-NAME',again)

    def exit_fixture(self, *, selected='Dead', mode='rule'):
        calls = []
        def request(method,path,payload=None):
            calls.append(path)
            if path=='/configs':
                return {'mode':mode}
            if path=='/rules':
                return {'rules':[{'type':'Match','proxy':'Main'}]}
            if path=='/proxies':
                return {'proxies':{
                    'GLOBAL':{'type':'Selector','all':['DIRECT','Main'],'now':'DIRECT'},
                    'DIRECT':{'type':'Direct'},
                    'Main':{'type':'Selector','all':['Auto','Dead','Good'],'now':'Auto'},
                    'Auto':{'type':'URLTest','all':['Dead','Good'],'now':selected},
                    'Dead':{'type':'VLESS'},'Good':{'type':'VLESS'}}}
            if path.startswith('/proxies/Dead/delay?'):
                raise ControllerError(504)
            if path.startswith('/proxies/Good/delay?') or path.startswith('/proxies/GLOBAL/delay?'):
                return {'delay':10}
            raise AssertionError('unexpected request')
        self.controller.request.side_effect=request
        return calls

    def test_rule_mode_tests_selected_leaf_even_when_global_direct_and_guard_group_differs(self):
        calls=self.exit_fixture()
        self.proxy.group='Unrelated Guard Group'
        self.assertIs(self.proxy.proxy_healthy(),False)
        self.assertEqual(self.proxy.egress,'Main → Auto → Dead')
        delays=[path for path in calls if '/delay?' in path]
        self.assertEqual(len(delays),2)
        self.assertTrue(all(path.startswith('/proxies/Dead/') and 'https%3A' in path for path in delays))

    def test_healthy_selected_exit_does_not_switch_proxy(self):
        self.exit_fixture(selected='Good')
        self.proxy.fail_open=Mock()
        self.proxy.tick(threading.Event(),Mock(),required_failures=1)
        self.assertEqual(self.proxy.state,'healthy')
        self.proxy.fail_open.assert_not_called()

    def test_explicit_network_test_switches_after_both_exit_probes_fail_and_direct_passes(self):
        self.exit_fixture()
        self.proxy.direct_healthy=Mock(return_value=True)
        self.proxy.fail_open=Mock(return_value=True)
        self.proxy.tick(threading.Event(),Mock(),required_failures=1)
        self.proxy.fail_open.assert_called_once()
        self.assertEqual(self.proxy.state,'direct')

    def test_controller_failure_or_unsupported_delay_is_unknown_not_exit_failure(self):
        calls=self.exit_fixture()
        original=self.controller.request.side_effect
        for status in (400,401,404,500):
            def request(method,path,payload=None):
                if '/delay?' in path:
                    raise ControllerError(status)
                return original(method,path,payload)
            self.controller.request.side_effect=request
            self.proxy.fail_open=Mock()
            self.proxy.direct_healthy=Mock(return_value=True)
            self.proxy.tick(threading.Event(),Mock(),required_failures=1)
            self.assertEqual(self.proxy.state,'uncertain')
            self.proxy.fail_open.assert_not_called()

    def test_direct_mode_and_selected_direct_never_test_global_as_a_proxy(self):
        calls=self.exit_fixture(mode='direct')
        self.assertIsNone(self.proxy.proxy_healthy())
        self.assertFalse(any('/delay?' in path for path in calls))
        calls=self.exit_fixture(selected='DIRECT')
        self.assertIsNone(self.proxy.proxy_healthy())
        self.assertFalse(any('/delay?' in path for path in calls))

    def test_selection_loop_is_unknown_and_preserves_network(self):
        self.exit_fixture(selected='Main')
        self.proxy.fail_open=Mock()
        self.proxy.tick(threading.Event(),Mock(),required_failures=1)
        self.assertEqual(self.proxy.state,'uncertain')
        self.proxy.fail_open.assert_not_called()

    def test_exit_changed_during_failed_probe_does_not_disable_new_exit(self):
        self.exit_fixture()
        original=self.controller.request.side_effect
        delays=[]
        def request(method,path,payload=None):
            if '/delay?' in path:
                delays.append(path)
            result=original(method,path,payload)
            if path=='/proxies' and len(delays)==2:
                result['proxies']['Auto']['now']='Good'
            return result
        self.controller.request.side_effect=request
        self.proxy.direct_healthy=Mock(return_value=True)
        self.proxy.fail_open=Mock()
        self.proxy.tick(threading.Event(),Mock(),required_failures=1)
        self.proxy.fail_open.assert_not_called()
        self.assertEqual(self.proxy.message,'出口已切换，等待重新检测')

    def test_diagnostic_check_with_switch_off_reports_bad_exit_without_mutation(self):
        self.exit_fixture()
        self.proxy.fail_open=Mock()
        self.proxy.direct_healthy=Mock()
        self.proxy.tick(threading.Event(),Mock(),required_failures=1,switch_allowed=lambda:False)
        self.assertEqual(self.proxy.state,'failed')
        self.proxy.fail_open.assert_not_called()
        self.proxy.direct_healthy.assert_not_called()

    def test_three_failed_rounds_and_positive_direct_proof_are_required(self):
        self.proxy.proxy_healthy = Mock(return_value=False)
        self.proxy.direct_healthy = Mock(return_value=True)
        self.proxy.fail_open = Mock(return_value=True)
        event = Mock()
        for _ in range(2):
            self.proxy.tick(threading.Event(),event)
        self.proxy.fail_open.assert_not_called()
        self.proxy.tick(threading.Event(),event)
        self.proxy.fail_open.assert_called_once()
        event.assert_called_once()

    def test_uncertain_direct_or_disabled_switch_never_changes_network(self):
        self.proxy.proxy_healthy = Mock(return_value=False)
        self.proxy.direct_healthy = Mock(return_value=False)
        self.proxy.fail_open = Mock()
        for _ in range(4):
            self.proxy.tick(threading.Event(),Mock())
        self.assertEqual(self.proxy.failures,0)
        self.proxy.fail_open.assert_not_called()
        self.proxy.direct_healthy.return_value = True
        self.proxy.tick(threading.Event(),Mock(),lambda:False)
        self.proxy.fail_open.assert_not_called()

    def test_fail_open_persists_snapshot_and_manual_restore_roundtrips(self):
        self.assertTrue(self.proxy.fail_open())
        self.assertEqual(self.registry['ProxyEnable'],0)
        self.assertIn(('PATCH','/configs',{'tun':{'enable':False}}),self.calls)
        self.assertIn('enable_tun_mode: false',(self.controller.root/'verge.yaml').read_text())
        self.assertNotIn('proxy', json.dumps(self.proxy.status()).lower().replace('restore_available',''))
        self.proxy.restore()
        self.assertEqual(self.registry['ProxyEnable'],1)
        self.assertFalse((self.root/'proxy-recovery.json').exists())
        self.assertIn(('PATCH','/configs',{'tun':{'enable':True}}),self.calls)
        self.assertIn('other: unchanged',(self.controller.root/'verge.yaml').read_text())

    def test_other_clients_system_proxy_is_preserved(self):
        self.registry['ProxyServer']='127.0.0.1:9999'
        self.proxy.fail_open()
        self.assertEqual(self.registry['ProxyEnable'],1)
        self.proxy.system_proxy.assert_called_once_with()

    def test_controller_failure_disables_only_owned_proxy_and_retries_tun(self):
        original = self.controller.request.side_effect
        def request(method,path,payload=None):
            if method == 'PATCH':
                raise OSError('offline')
            return original(method,path,payload)
        self.controller.request.side_effect=request
        with self.assertRaises(OSError):
            self.proxy.fail_open()
        self.assertEqual(self.registry['ProxyEnable'],0)
        saved = json.loads((self.root/'proxy-recovery.json').read_text())
        self.assertFalse(saved['tun_disabled'])
        self.controller.request.side_effect = original
        self.assertTrue(self.proxy.fail_open())
        self.proxy.restore()
        self.assertEqual(self.registry['ProxyEnable'],1)

    def test_restore_does_not_overwrite_a_new_system_proxy(self):
        self.proxy.fail_open()
        self.registry.update(ProxyEnable=1,ProxyServer='127.0.0.1:9999')
        with self.assertRaises(ValueError):
            self.proxy.restore()
        self.assertEqual(self.registry['ProxyServer'],'127.0.0.1:9999')
        self.assertTrue((self.root/'proxy-recovery.json').exists())

    def test_demo_never_calls_controller_or_system_switches(self):
        self.proxy.demo = True
        self.proxy.configure([self.exe],identifier=self.identifier,mode='direct')
        self.proxy.tick(threading.Event(),Mock())
        self.controller.request.assert_not_called()
        self.proxy.system_proxy.assert_not_called()

    def test_client_profiles_and_recovery_snapshots_are_separate(self):
        self.proxy.demo=True
        self.proxy.configure([self.exe],identifier=self.identifier,mode='direct')
        verge_policies=self.proxy.policies.copy()
        verge_snapshot=self.proxy.recovery_path
        self.proxy.set_client('flclash')
        self.assertEqual(self.proxy.policies,{})
        self.assertNotEqual(self.proxy.recovery_path,verge_snapshot)
        self.proxy.configure([self.exe],identifier=self.identifier,mode='direct')
        reopened=ProxyProtection(self.root,demo=True)
        self.assertEqual(reopened.client,'flclash')
        self.assertEqual(reopened.controller.runtime_path.name,'config.yaml')
        reopened.set_client('clash-verge')
        self.assertEqual(reopened.policies,verge_policies)

    def prepare_flclash(self):
        self.proxy.client='flclash'
        self.controller.kind='flclash'
        self.controller.runtime_path=self.controller.root/'config.yaml'
        self.controller.runtime_path.write_text(self.runtime)
        prefs={'flutter.config':json.dumps({'networkProps':{'systemProxy':True,'untouched':'network'},'patchClashConfig':{'tun':{'enable':True,'device':'fixture-device'},'secret':'private-fixture'},'currentProfileId':17}), 'flutter.version':90,'unrelated':'retain'}
        (self.controller.root/'shared_preferences.json').write_text(json.dumps(prefs))

    def test_flclash_switch_and_restore_preserve_all_other_preferences(self):
        self.prepare_flclash()
        prefs_path=self.controller.root/'shared_preferences.json'
        original=json.loads(prefs_path.read_text())
        self.proxy.fail_open()
        saved=json.loads(prefs_path.read_text())
        config=json.loads(saved['flutter.config'])
        self.assertFalse(config['networkProps']['systemProxy'])
        self.assertFalse(config['patchClashConfig']['tun']['enable'])
        self.assertEqual(config['patchClashConfig']['tun']['device'],'fixture-device')
        self.assertEqual(config['currentProfileId'],17)
        self.assertEqual(saved['unrelated'],'retain')
        self.proxy.restore()
        restored=json.loads(prefs_path.read_text())
        self.assertEqual(json.loads(restored['flutter.config']),json.loads(original['flutter.config']))
        self.assertEqual(restored['flutter.version'],original['flutter.version'])

    def test_flclash_rules_write_runtime_and_reapply_after_client_regeneration(self):
        self.prepare_flclash()
        script_before=(self.controller.root/'profiles/Script.js').read_bytes()
        self.proxy.configure([self.exe],identifier=self.identifier,mode='direct')
        self.assertIn('PROCESS-PATH',self.controller.runtime_path.read_text())
        self.assertEqual((self.controller.root/'profiles/Script.js').read_bytes(),script_before)
        self.assertTrue((self.root/'proxy-flclash-original.yaml').exists())
        self.controller.runtime_path.write_text(self.runtime)
        original=self.controller.request.side_effect
        def request(method,path,payload=None):
            if path=='/rules':
                return {'rules':[{'type':'Match','proxy':'DIRECT'}]}
            return original(method,path,payload)
        self.controller.request.side_effect=request
        self.proxy.ensure_rules([self.exe])
        self.assertIn('PROCESS-PATH',self.controller.runtime_path.read_text())

    def test_rule_reload_preserves_live_tun_and_selected_node(self):
        self.prepare_flclash()
        self.config['tun']['enable']=False
        original=self.controller.request.side_effect
        def request(method,path,payload=None):
            if path=='/proxies':
                return {'proxies':{'Sample':{'type':'Selector','all':['first','chosen'],'now':'chosen'}}}
            return original(method,path,payload)
        self.controller.request.side_effect=request
        self.proxy.configure([self.exe],identifier=self.identifier,mode='direct')
        payload=next(call[2]['payload'] for call in self.calls if call[0]=='PUT' and call[1]=='/configs')
        self.assertIn('tun: {"enable": false}',payload)
        self.assertIn(('PUT','/proxies/Sample',{'name':'chosen'}),self.calls)


class HttpControllerTests(unittest.TestCase):
    def test_flclash_http_uses_local_token_without_environment_proxy(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):
                pass
            def do_GET(self):
                if self.headers.get('Authorization')!='Bearer fixture-token':
                    self.send_response(401); self.end_headers(); return
                body=json.dumps({'meta':True,'version':'fixture'}).encode()
                self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root=Path(directory)
                (root/'config.yaml').write_text(f'external-controller: 127.0.0.1:{server.server_port}\nsecret: fixture-token\n')
                with patch.dict('os.environ',{'HTTP_PROXY':'http://127.0.0.1:1'}):
                    result=Controller(root,kind='flclash').request('GET','/version')
                self.assertEqual(result['version'],'fixture')
                # UI controller settings may be newer than its generated YAML.
                (root/'config.yaml').write_text('external-controller: ""\nsecret: old-token\n')
                (root/'shared_preferences.json').write_text(json.dumps({'flutter.config':json.dumps({'patchClashConfig':{'external-controller':f'127.0.0.1:{server.server_port}','secret':'fixture-token'}})}))
                self.assertEqual(Controller(root,kind='flclash').request('GET','/version')['version'],'fixture')
                (root/'shared_preferences.json').unlink()
                (root/'config.yaml').write_text('external-controller: example.invalid:9090\nsecret: fixture-token\n')
                with self.assertRaises(OSError):
                    Controller(root,kind='flclash').request('GET','/version')
        finally:
            server.shutdown();server.server_close();worker.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
