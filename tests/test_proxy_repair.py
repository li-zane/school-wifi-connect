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

if __name__ == '__main__':
    unittest.main()
