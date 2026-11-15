import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from njust_wifi.service import Service
from njust_wifi.storage import Store
from njust_wifi.probes import ApplicationGuards


class AuditTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.service = Service(Store(self.root), demo=True)
        self.addCleanup(self.service.close)

    def test_action_audit_names_software_and_redacts_diagnostic_export(self):
        exe = self.root / 'GameViewer.exe'
        exe.touch()
        self.service.guards.add([str(exe)])
        operation = self.service.operation('/api/proxy/policy',
            {'id':self.service.guards.identifier(exe), 'mode':'direct', 'secret':'do-not-log'})
        self.service.audit(operation)
        self.assertIn('GameViewer', self.service.logs()[-1]['message'])
        self.assertIn('直连', self.service.logs()[-1]['message'])
        disk = (self.root / 'events.jsonl').read_text('utf-8')
        self.assertNotIn(str(self.root), disk)
        self.assertNotIn('do-not-log', disk)
        self.assertNotIn('GameViewer', json.dumps(self.service.diagnostics()))
        reopened = Service(Store(self.root), demo=True)
        try:
            self.assertNotIn('GameViewer', json.dumps(reopened.diagnostics()))
            self.assertIn('GameViewer', reopened.logs()[-1]['message'])
        finally:
            reopened.close()

    def test_config_and_failure_audit_never_records_input_values(self):
        operation = self.service.operation('/api/config', {'username':'secret-name',
            'password':'secret-pass', 'fallback_networks':[{'ssid':'private-ssid','password':'private-pass'}]})
        self.service.audit(operation, False)
        entry = self.service.logs()[-1]
        self.assertEqual(entry['level'], 'error')
        self.assertIn('认证账号', entry['message'])
        for secret in ('secret-name','secret-pass','private-ssid','private-pass'):
            self.assertNotIn(secret, json.dumps(self.service.logs()))

    def test_proxy_health_reuses_duration_and_repeated_fault_is_logged_once(self):
        self.service.proxy.state = 'healthy'
        self.service.proxy.message = '代理出口正常'
        for _ in range(5):
            self.service.observe_proxy()
        self.assertEqual(len(self.service.logs()), 1)
        self.assertTrue(self.service.logs()[0]['active'])
        self.service.proxy.state = 'error'
        self.service.proxy.message = '系统代理：客户端开关与实际网络状态不一致'
        for _ in range(5):
            self.service.observe_proxy()
        self.assertEqual(len(self.service.logs()), 2)
        self.assertFalse(self.service.logs()[0]['active'])
        self.service.observe_proxy(requested=True)
        self.assertEqual(len(self.service.logs()), 3)

    def test_guard_health_and_restart_messages_use_software_name(self):
        exe = self.root / 'GameViewer.exe'
        exe.touch()
        guard = self.service.guards
        guard.paths = [exe]
        event, health = Mock(), Mock()
        with patch('njust_wifi.probes.subprocess.run', return_value=Mock(returncode=0, stdout=b'"GameViewer.exe","1"\r\n')):
            guard.check(threading.Event(), event, health)
        self.assertIn('GameViewer', health.call_args.args[1])
        self.assertNotIn('守护程序 1', health.call_args.args[1])
        with patch('njust_wifi.probes.subprocess.run', side_effect=[
                Mock(returncode=0, stdout=b''), Mock(returncode=1, stdout=b'')]), \
             patch('njust_wifi.probes.subprocess.Popen'):
            guard.check(threading.Event(), event, health)
        self.assertTrue(all('GameViewer' in c.args[1] for c in event.call_args_list))

if __name__ == '__main__':
    unittest.main()
