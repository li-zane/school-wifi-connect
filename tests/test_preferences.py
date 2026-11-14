from pathlib import Path
import tempfile
import unittest
import time
from unittest.mock import Mock

from njust_wifi.storage import Store, validate_settings
from njust_wifi.service import Service


class PreferencesTests(unittest.TestCase):
    def test_normal_network_check_also_checks_proxy_exit_when_protection_enabled(self):
        with tempfile.TemporaryDirectory() as temporary:
            store=Store(Path(temporary))
            store.update({'username':'sample','password':'sample-password'})
            network=Mock()
            service=Service(store,network_factory=lambda _:network)
            try:
                service.proxy.tick=Mock()
                service.refresh_balance=Mock()
                store.settings['proxy_fail_open']=True
                service.start(once=True)
                deadline=time.monotonic()+3
                while (service.checked_at is None or service.busy) and time.monotonic()<deadline:
                    time.sleep(.01)
                self.assertEqual(service.status()['state'],'online')
                network.recover.assert_called_once()
                self.assertTrue(any(call.kwargs.get('required_failures')==1 for call in service.proxy.tick.call_args_list))
            finally:
                service.close()

    def test_proxy_check_request_is_async_and_keeps_the_switch_off(self):
        with tempfile.TemporaryDirectory() as temporary:
            service=Service(Store(Path(temporary)),demo=True)
            try:
                service.proxy.tick=Mock()
                service.proxy_action('check',{})
                deadline=time.monotonic()+2
                while not service.proxy.tick.called and time.monotonic()<deadline:
                    time.sleep(.01)
                self.assertTrue(service.proxy.tick.called)
                call=service.proxy.tick.call_args
                self.assertEqual(call.kwargs['required_failures'],1)
                self.assertFalse(call.kwargs['switch_allowed']())
                self.assertFalse(service.store.settings['proxy_fail_open'])
            finally:
                service.close()

    def test_legacy_settings_keep_ask_and_proxy_protection_off(self):
        self.assertEqual(validate_settings({'ssid':'NJUST'})['close_behavior'],'ask')
        self.assertFalse(validate_settings({})['proxy_fail_open'])
        for value in ('tray','exit','ask'):
            self.assertEqual(validate_settings({'close_behavior':value})['close_behavior'],value)
        for raw in ({'close_behavior':'other'},{'proxy_fail_open':'yes'},{'close_behavior':[]}):
            with self.assertRaises(ValueError):
                validate_settings(raw)

    def test_partial_autosaves_preserve_credentials_and_do_not_wake_network(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = Store(Path(temporary))
            store.update({'username':'sample','password':'sample-password','remember_credentials':True})
            service = Service(store,demo=True)
            try:
                service.running=True
                service.busy=True
                service.wake.clear()
                service.configure({'close_behavior':'tray'})
                self.assertFalse(service.wake.is_set())
                service.configure({'domain':'default','interval_minutes':9})
                self.assertTrue(service.wake.is_set())
                reopened=Store(store.root)
                self.assertEqual(reopened.settings['close_behavior'],'tray')
                self.assertEqual(reopened.password,'sample-password')
                self.assertEqual(reopened.settings['interval_minutes'],9)
            finally:
                service.close()
