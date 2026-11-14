import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock

from njust_wifi.proxy import ProxyProtection, apply_rules, make_script, strip_script
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


if __name__ == '__main__':
    unittest.main()
