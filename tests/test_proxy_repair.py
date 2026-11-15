import json
import threading
import unittest
from unittest.mock import Mock

import test_proxy


class ProxyRepairTests(unittest.TestCase):
    setUp = test_proxy.ProxyTests.setUp
    write_client_flags = test_proxy.ProxyTests.write_client_flags

    def test_explicit_restore_repairs_stale_on_indicator_through_client(self):
        self.proxy.fail_open()
        self.gui.update(proxy=True, tun=True)
        self.config['tun']['enable'] = True
        self.write_client_flags()
        self.switches.set.reset_mock()
        self.proxy.restore()
        self.assertEqual(self.registry['ProxyEnable'], 1)
        self.assertEqual([c.args for c in self.switches.set.call_args_list],
                         [('proxy', True, False), ('proxy', False, True)])
        self.assertFalse(self.proxy.recovery_path.exists())

    def test_manual_restore_retires_old_record_and_resumes_egress_checks(self):
        self.proxy.fail_open()
        self.gui.update(proxy=True, tun=True)
        self.config['tun']['enable'] = True
        self.registry['ProxyEnable'] = 1
        self.write_client_flags()
        self.switches.set.reset_mock()
        self.proxy.tick(threading.Event(), Mock())
        self.assertFalse(self.proxy.recovery_path.exists())
        self.assertEqual(self.proxy.state, 'restored')
        self.proxy.proxy_healthy = Mock(return_value=True)
        self.proxy.tick(threading.Event(), Mock())
        self.proxy.proxy_healthy.assert_called_once()
        self.assertEqual(self.proxy.state, 'healthy')
        self.switches.set.assert_not_called()

    def test_stale_indicator_is_reported_with_field_and_never_auto_pulsed(self):
        self.proxy.fail_open()
        self.gui.update(proxy=True, tun=True)
        self.config['tun']['enable'] = True
        self.write_client_flags()
        self.switches.set.reset_mock()
        self.proxy.tick(threading.Event(), Mock())
        self.assertIn('系统代理', self.proxy.message)
        self.assertEqual(self.proxy.state, 'error')
        self.switches.set.assert_not_called()
        self.assertTrue(self.proxy.recovery_path.exists())

    def test_failed_reapply_can_resume_without_blind_repeated_toggle(self):
        self.proxy.fail_open()
        self.gui['proxy'] = True
        self.write_client_flags()
        real = self.switches.set.side_effect
        def lost_reply(field, expected, desired):
            result = real(field, expected, desired)
            if field == 'proxy' and not desired:
                raise TimeoutError('lost reply')
            return result
        self.switches.set.side_effect = lost_reply
        with self.assertRaises(ValueError):
            self.proxy.restore()
        self.switches.set.side_effect = real
        self.switches.set.reset_mock()
        self.proxy.restore()
        self.assertEqual(self.gui, {'proxy': True, 'tun': True})
        self.assertFalse(any(c.args == ('proxy', True, False) for c in self.switches.set.call_args_list))

    def test_foreign_proxy_blocks_stale_indicator_repair_before_any_switch(self):
        self.proxy.fail_open()
        self.gui['proxy'] = True
        self.registry.update(ProxyEnable=1, ProxyServer='127.0.0.1:9999')
        self.write_client_flags()
        self.switches.set.reset_mock()
        with self.assertRaises(ValueError):
            self.proxy.restore()
        self.switches.set.assert_not_called()
        self.assertEqual(self.registry['ProxyServer'], '127.0.0.1:9999')

    def test_bad_exit_with_client_switches_off_never_operates_client_window(self):
        self.gui.update(proxy=False, tun=False)
        self.registry['ProxyEnable'] = 0
        self.config['tun']['enable'] = False
        self.write_client_flags()
        self.proxy.proxy_healthy = Mock(return_value=False)
        self.proxy.direct_healthy = Mock(return_value=True)
        for _ in range(5):
            self.proxy.tick(threading.Event(), Mock())
        self.assertEqual(self.proxy.state, 'idle')
        self.switches.read.assert_not_called()
        self.assertFalse(self.proxy.recovery_path.exists())

    def test_stale_saved_switch_still_reaches_the_client_for_fail_open(self):
        (self.controller.root/'verge.yaml').write_text('enable_system_proxy: false\nenable_tun_mode: false\n')
        self.assertTrue(self.proxy.fail_open())
        self.assertEqual(self.gui, {'proxy': False, 'tun': False})

    def test_unresolved_record_is_reported_without_operating_client_each_tick(self):
        self.proxy.fail_open()
        self.gui['tun'] = True
        self.config['tun']['enable'] = True
        self.write_client_flags()
        self.switches.read.reset_mock()
        for _ in range(3):
            self.proxy.tick(threading.Event(), Mock())
        self.assertEqual(self.proxy.message, '恢复记录与客户端状态不一致，请点击恢复代理')
        self.switches.read.assert_not_called()
        self.assertTrue(self.proxy.recovery_path.exists())

    def test_elevated_client_is_reported_with_its_own_message(self):
        from njust_wifi.client_control import ClientControlError, ClientSwitches
        from unittest.mock import patch
        subject = ClientSwitches(self.root, 'flclash')
        subject._executable = Mock(return_value='bridge.exe')
        reply = Mock(returncode=1, stdout=b'{"ok":false,"error":"client_elevated"}')
        with patch('njust_wifi.client_control.subprocess.run', return_value=reply):
            with self.assertRaises(ClientControlError) as raised:
                subject.read()
        self.assertIn('管理员', str(raised.exception))

if __name__ == '__main__':
    unittest.main()
