from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from njust_wifi.boot import BootWorker, boot_configuration, current_sid, network_lease, read_json
from njust_wifi.network import Network
from njust_wifi.service import set_startup, Service
from njust_wifi.storage import Store


class BootTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = {'ssid':'fixture-campus', 'startup_enabled':True, 'auto_start_monitor':True,
                         'remember_credentials':True, 'show_portal':True}
        self.write_settings()
        (self.root/'credentials.json').write_text(json.dumps({'username':'fixture-user','password':'fixture-secret'}),'utf-8')
        self.network = Mock()
        self.factory = Mock(return_value=self.network)
        self.session = Mock(return_value=False)
        self.seconds = 100.0
        self.lease_calls = []
        @contextmanager
        def lease(cancel,owner):
            self.lease_calls.append(owner)
            yield
        self.worker = BootWorker({'owner_sid':'S-1-5-21-1-2-3-1001','data_directory':str(self.root)},
            network_factory=self.factory, session_check=self.session, lease=lease, clock=lambda:self.seconds)

    def write_settings(self):
        (self.root/'settings.json').write_text(json.dumps(self.settings),'utf-8')

    def test_prelogin_network_uses_saved_credentials_and_forbids_browser(self):
        original = {p.name:p.read_bytes() for p in self.root.iterdir()}
        self.assertEqual(self.worker.cycle(),300)
        self.factory.assert_called_once_with(self.worker.cancel,all_users=True)
        args=self.network.recover.call_args.args
        self.assertFalse(args[0]['show_portal'])
        self.assertEqual(args[1:3],('fixture-user','fixture-secret'))
        self.assertEqual(len(self.lease_calls),1)
        self.assertEqual(self.worker.state['state'],'online')
        self.assertNotIn('fixture-secret',json.dumps(self.worker.state))
        self.assertEqual({p.name:p.read_bytes() for p in self.root.iterdir()},original)

    def test_logged_in_or_locked_session_defers_without_touching_network(self):
        self.session.return_value=True
        self.assertEqual(self.worker.cycle(),10)
        self.factory.assert_not_called()
        self.assertEqual(self.worker.state['state'],'user_session')

    def test_login_between_config_and_lease_cancels_before_network(self):
        self.session.side_effect=[False,False,True]
        self.assertEqual(self.worker.cycle(),1)
        self.factory.assert_not_called()
        self.assertEqual(self.worker.state['state'],'handoff')

    def test_login_during_recovery_sets_cancel_and_hands_off(self):
        started = threading.Event()
        self.session.side_effect=lambda _:started.is_set()
        def recover(*_):
            started.set()
            self.assertTrue(self.worker.cancel.wait(3))
            raise InterruptedError()
        self.network.recover.side_effect=recover
        self.assertEqual(self.worker.cycle(),1)
        self.assertEqual(self.worker.state['state'],'handoff')

    def test_disabling_startup_or_auto_monitor_prevents_recovery(self):
        for key in ('startup_enabled','auto_start_monitor'):
            with self.subTest(key=key):
                self.settings[key]=False;self.write_settings()
                self.worker.cycle()
                self.assertEqual(self.worker.state['state'],'disabled')
                self.settings[key]=True
        self.factory.assert_not_called()

    def test_session_only_credentials_are_never_used_by_boot(self):
        self.settings['remember_credentials']=False;self.write_settings()
        self.worker.cycle()
        self.assertEqual(self.worker.state['state'],'disabled')
        self.factory.assert_not_called()

    def test_missing_credentials_still_permits_configured_fallback(self):
        (self.root/'credentials.json').unlink()
        self.settings['fallback_networks']=[{'ssid':'fixture-backup','password':'fixture-pass'}]
        self.write_settings()
        settings,user,password,active=boot_configuration(self.root)
        self.assertTrue(active)
        self.assertEqual((user,password),('',''))
        self.assertEqual(settings['fallback_networks'][0]['ssid'],'fixture-backup')

    def test_failure_backoff_is_bounded_and_does_not_expose_errors(self):
        self.network.recover.side_effect=RuntimeError('private fixture secret')
        self.assertEqual([self.worker.cycle() for _ in range(7)],[30,60,120,240,300,300,300])
        self.assertNotIn('private fixture secret',json.dumps(self.worker.state))

    def test_normal_duration_updates_without_adding_normal_events(self):
        self.worker.cycle();self.seconds+=90120;self.worker.cycle()
        self.assertEqual(self.worker.state['duration_seconds'],90120)
        self.assertEqual(self.worker.events,[])

    def test_unreadable_session_never_operates_network(self):
        self.session.side_effect=OSError('fixture session unavailable')
        self.worker.cycle()
        self.factory.assert_not_called()

    def test_config_reload_applies_new_network_order_on_next_cycle(self):
        self.worker.cycle()
        self.settings['fallback_networks']=[{'ssid':'first-fixture','password':''},{'ssid':'second-fixture','password':''}]
        self.write_settings();self.worker.cycle()
        self.assertEqual(self.network.recover.call_args.args[0]['fallback_networks'],self.settings['fallback_networks'])

    def test_oversized_or_nonobject_configuration_is_rejected(self):
        p=self.root/'oversized.json';p.write_text(' '*65537,'utf-8')
        with self.assertRaises(ValueError):read_json(p)
        p.write_text('[]','utf-8')
        with self.assertRaises(ValueError):read_json(p)

    def test_boot_profile_creation_is_explicitly_all_users(self):
        cancel=Mock();cancel.is_set.return_value=False;cancel.wait.return_value=False
        n=Network(cancel,all_users=True)
        n.netsh=Mock();n.current_ssid=Mock(return_value='fixture-wifi')
        with patch('njust_wifi.network.install_profile') as install:
            n.connect('fixture-wifi',password='fixture-pass')
            install.assert_called_once_with('fixture-wifi','fixture-pass',all_users=True)

    @unittest.skipUnless(os.name=='nt','Windows named mutex integration')
    def test_native_network_lease_serializes_and_honors_cancel(self):
        entered=threading.Event();cancel=threading.Event();done=threading.Event()
        def second():
            try:
                with network_lease(cancel,current_sid()):entered.set()
            except InterruptedError:pass
            finally:done.set()
        with network_lease(threading.Event()):
            thread=threading.Thread(target=second);thread.start()
            self.assertFalse(entered.wait(.3));cancel.set()
            self.assertTrue(done.wait(2));self.assertFalse(entered.is_set())
        thread.join()

    def test_frozen_startup_installs_boot_task_before_login_trigger(self):
        exe=self.root/'NJUSTWiFiBackend.exe';exe.write_bytes(b'fixture')
        (self.root/'Boot-Network.ps1').write_text('fixture','utf-8')
        with patch('njust_wifi.service.sys.frozen',True,create=True),patch('njust_wifi.service.sys.executable',str(exe)),patch('njust_wifi.service.subprocess.run') as run:
            run.side_effect=[Mock(returncode=1),Mock(returncode=0),Mock(returncode=0)]
            set_startup(True)
        commands=[c.args[0] for c in run.call_args_list]
        self.assertIn(str(self.root/'Boot-Network.ps1'),commands[1])
        self.assertEqual(commands[1][commands[1].index('-Mode')+1],'Install')
        self.assertNotIn('-NoLogon',commands[2])

    def test_declined_boot_setup_never_changes_existing_login_task(self):
        exe=self.root/'NJUSTWiFiBackend.exe';exe.write_bytes(b'fixture')
        (self.root/'Boot-Network.ps1').write_text('fixture','utf-8')
        with patch('njust_wifi.service.sys.frozen',True,create=True),patch('njust_wifi.service.sys.executable',str(exe)),patch('njust_wifi.service.subprocess.run') as run:
            run.side_effect=[Mock(returncode=1),Mock(returncode=1)]
            with self.assertRaises(ValueError):set_startup(True)
        self.assertEqual(run.call_count,2)

    def test_failed_startup_setup_restores_preference(self):
        store=Store(self.root);store.update({'startup_enabled':False})
        service=Service(store,demo=False)
        try:
            with patch('njust_wifi.service.set_startup',side_effect=ValueError('fixture UAC canceled')):
                with self.assertRaises(ValueError):service.configure({'startup_enabled':True})
            self.assertFalse(Store(self.root).settings['startup_enabled'])
        finally:service.close()
