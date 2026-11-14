import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from xml.etree import ElementTree

from njust_wifi.network import Network
from njust_wifi.service import Service
from njust_wifi.storage import Store, validate_settings
from njust_wifi.wlan import profile_xml, profile_password


class FallbackTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch('njust_wifi.network.restore_wifi_radio', return_value=False))

    def test_saved_profile_key_must_be_plaintext_and_match_the_configured_ssid(self):
        xml = profile_xml('fixture & wifi', 'fixture-pass')
        self.assertEqual(profile_password(xml, 'fixture & wifi'), 'fixture-pass')
        self.assertEqual(profile_password(xml, 'another wifi'), '')
        self.assertEqual(profile_password(xml.replace('<protected>false', '<protected>true'), 'fixture & wifi'), '')
        self.assertEqual(profile_password(profile_xml('open wifi'), 'open wifi'), '')
        self.assertEqual(profile_password('<broken', 'fixture & wifi'), '')

    def test_migrates_single_backup_without_losing_settings(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'settings.json').write_text(json.dumps({'ssid': 'NJUST', 'fallback_ssid': 'legacy-fixture', 'interval_minutes': 7}))
            store = Store(root)
            self.assertEqual(store.settings['fallback_networks'], [{'ssid': 'legacy-fixture', 'password': ''}])
            self.assertNotIn('fallback_ssid', store.public())
            store.update({'interval_minutes': 8})
            self.assertEqual(Store(root).settings['fallback_networks'][0]['ssid'], 'legacy-fixture')
            self.assertNotIn('fallback_ssid', json.loads(store.settings_path.read_text()))

    def test_multiple_passwords_order_and_plaintext_roundtrip(self):
        networks = [{'ssid': 'fixture-a', 'password': 'secret123'}, {'ssid': 'fixture-b', 'password': ''}]
        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp))
            store.update({'ssid': '', 'fallback_networks': networks})
            self.assertEqual(Store(store.root).settings['fallback_networks'], networks)
            self.assertEqual(store.public()['fallback_networks'], networks)
            store.update({'fallback_networks': networks[::-1]})
            self.assertEqual(Store(store.root).settings['fallback_networks'], networks[::-1])

    def test_rejects_duplicate_or_invalid_wifi_passwords(self):
        for networks in ([{'ssid': 'a', 'password': 'short'}], [{'ssid': 'a', 'password': '秘密秘密秘密秘密'}],
                         [{'ssid': 'a', 'password': ''}, {'ssid': 'A', 'password': ''}],
                         [{'ssid': '', 'password': ''}], [{'ssid': 'NJUST', 'password': ''}],
                         [{'ssid': 'a', 'password': '', 'other': True}], [{'ssid': 'a', 'password': ''}] * 9):
            with self.subTest(networks=networks), self.assertRaises(ValueError):
                validate_settings({'fallback_networks': networks})
        self.assertEqual(validate_settings({'fallback_networks': [{'ssid': 'a', 'password': 'a' * 64}]})['fallback_networks'][0]['password'], 'a' * 64)

    def network(self):
        network = Network(threading.Event())
        network.current_ssid = Mock(return_value='')
        network.connect = Mock()
        network.login = Mock()
        return network

    def test_campus_empty_uses_all_backups_in_exact_order_without_login(self):
        network = self.network()
        network.online = Mock(side_effect=[False, False, True])
        network.recover({'ssid': '', 'domain': 'default', 'fallback_networks':
            [{'ssid': 'first', 'password': 'first-secret'}, {'ssid': 'second', 'password': 'second-secret'}]}, '', '', Mock())
        self.assertEqual(network.connect.call_args_list[0].args, ('first',))
        self.assertEqual(network.connect.call_args_list[0].kwargs, {'password': 'first-secret'})
        self.assertEqual(network.connect.call_args_list[1].args, ('second',))
        network.login.assert_not_called()

    def test_failed_backup_does_not_skip_later_candidates(self):
        network = self.network()
        network.online = Mock(side_effect=[False, True])
        network.connect.side_effect = [RuntimeError('Wi-Fi 操作失败，请检查系统网络配置'), None]
        network.recover({'ssid': '', 'fallback_networks': [{'ssid': 'first', 'password': ''}, {'ssid': 'second', 'password': ''}]}, '', '', Mock())
        self.assertEqual([c.args[0] for c in network.connect.call_args_list], ['first', 'second'])

    def test_campus_empty_chooses_backup_even_if_unrelated_wifi_is_online(self):
        network = self.network()
        network.current_ssid.return_value = 'unrelated'
        network.online = Mock(return_value=True)
        network.recover({'ssid': '', 'fallback_networks': [{'ssid': 'first', 'password': ''}]}, '', '', Mock())
        network.connect.assert_called_once_with('first', password='')
        network.login.assert_not_called()

    def test_campus_failure_then_ordered_backups(self):
        network = self.network()
        network.online = Mock(side_effect=[False, False, False, True])
        network.login.side_effect = RuntimeError('校园认证失败，请检查账号、密码和认证域')
        network.recover({'ssid': 'NJUST', 'domain': 'default', 'fallback_networks':
            [{'ssid': 'first', 'password': ''}, {'ssid': 'second', 'password': ''}]}, 'u', 'p', Mock())
        self.assertEqual([c.args[0] for c in network.connect.call_args_list], ['NJUST', 'first', 'second'])
        network.login.assert_called_once_with('u', 'p', 'default')

    def test_healthy_first_backup_does_not_reconnect_and_lower_returns_to_first(self):
        settings = {'ssid': '', 'fallback_networks': [{'ssid': 'first', 'password': ''}, {'ssid': 'second', 'password': ''}]}
        network = self.network()
        network.current_ssid.return_value = 'first'
        network.online = Mock(return_value=True)
        network.recover(settings, '', '', Mock())
        network.connect.assert_not_called()
        network.current_ssid.return_value = 'second'
        network.recover(settings, '', '', Mock())
        network.connect.assert_called_once_with('first', password='')

    def test_no_campus_credentials_still_allows_fallback_only_monitor(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp))
            store.update({'ssid': '', 'fallback_networks': [{'ssid': 'first', 'password': ''}]})
            service = Service(store, network_factory=lambda _: Mock())
            try:
                service.start(once=True)
                self.assertTrue(service.wake.wait(1) or service.busy or service.checked_at)
            finally:
                service.close()

    def test_password_profile_is_in_memory_and_escaped(self):
        xml = profile_xml('fixture & WiFi', 'a<&"12345')
        root = ElementTree.fromstring(xml)
        ns = {'w': 'http://www.microsoft.com/networking/WLAN/profile/v1'}
        self.assertEqual(root.find('w:MSM/w:security/w:sharedKey/w:keyMaterial', ns).text, 'a<&"12345')
        self.assertEqual(root.find('w:SSIDConfig/w:SSID/w:hex', ns).text, 'fixture & WiFi'.encode().hex())
        self.assertIn('<connectionMode>manual</connectionMode>', xml)
        network = Network(threading.Event())
        network.netsh = Mock()
        network.current_ssid = Mock(return_value='fixture')
        network.cancel.wait = Mock(return_value=False)
        with patch('njust_wifi.network.install_profile') as install:
            network.connect('fixture', password='password123')
            install.assert_called_once_with('fixture', 'password123')
        self.assertFalse(any('password123' in str(call) for call in network.netsh.call_args_list))

    def test_cancel_does_not_write_wifi_password_profile(self):
        network = Network(threading.Event())
        network.cancel.set()
        with patch('njust_wifi.network.install_profile') as install:
            with self.assertRaises(InterruptedError):
                network.connect('fixture', password='password123')
            install.assert_not_called()
