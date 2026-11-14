from pathlib import Path
import tempfile
import unittest

from njust_wifi.storage import Store, validate_settings
from njust_wifi.service import Service


class PreferencesTests(unittest.TestCase):
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
