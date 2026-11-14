import ctypes
import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.request

from njust_wifi.network import Network, NoRedirect, direct_opener
from njust_wifi.server import Server
from njust_wifi.service import Service
from njust_wifi.storage import Store, validate_settings


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name))

    def test_remembered_secret_is_local_json_and_roundtrips(self):
        self.store.update({"username": "sample-user", "password": "sample-secret", "remember_credentials": True})
        self.assertEqual(json.loads(self.store.secret_path.read_text())["password"], "sample-secret")
        self.assertFalse(self.store.legacy_secret_path.exists())
        self.assertNotIn("sample-user", self.store.settings_path.read_text())
        reopened = Store(self.store.root)
        self.assertEqual(reopened.password, "sample-secret")
        self.assertEqual(reopened.public()["username_masked"], "sa***er")
        self.assertNotIn("password", reopened.public())
        self.assertNotIn("username", reopened.public())
        reopened.forget()
        self.assertFalse(reopened.secret_path.exists())
        self.assertFalse(Store(self.store.root).public()["has_credentials"])

    def test_legacy_credentials_migrate_once_without_losing_account(self):
        self.store.update({"remember_credentials": True})
        self.store.legacy_secret_path.write_bytes(b"old-windows-fixture")
        with patch("njust_wifi.storage.read_legacy_credentials", return_value={"username": "sample-user", "password": "sample-secret"}) as reader:
            reopened = Store(self.store.root)
            reader.assert_called_once()
        self.assertEqual(reopened.password, "sample-secret")
        self.assertFalse(reopened.legacy_secret_path.exists())
        self.assertEqual(Store(self.store.root).password, "sample-secret")

    def test_startup_and_auto_monitor_are_independent_and_compatible(self):
        self.store.update({"startup_enabled": True, "auto_start_monitor": False})
        self.assertTrue(Store(self.store.root).settings["startup_enabled"])
        self.assertFalse(Store(self.store.root).settings["auto_start_monitor"])
        old = {"startup_enabled": True}
        self.store.settings_path.write_text(json.dumps(old))
        self.assertTrue(Store(self.store.root).settings["auto_start_monitor"])

    def test_session_only_never_writes_credentials(self):
        self.store.update({"username": "session-user", "password": "session-secret"})
        self.assertFalse(self.store.secret_path.exists())
        self.assertEqual(Store(self.store.root).password, "")
        self.assertEqual(self.store.password, "session-secret")

    def test_empty_credentials_preserve_but_switching_username_needs_password(self):
        self.store.update({"username": "sample-user", "password": "sample-secret"})
        self.store.update({"username": "", "password": "", "interval_minutes": 8})
        self.assertEqual(self.store.password, "sample-secret")
        with self.assertRaises(ValueError):
            self.store.update({"username": "new-user"})

    def test_validation_rejects_unsafe_settings(self):
        for bad in ({"interval_minutes": 0}, {"interval_minutes": float("nan")},
                    {"ssid": "x\n"}, {"domain": "other host"}, {"unknown": True},
                    {"startup_enabled": "true"}, {"ssid": "中" * 32}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_settings(bad)


class NetworkTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch('njust_wifi.network.restore_wifi_radio', return_value=False))

    def network(self):
        return Network(threading.Event())

    def test_login_matches_portal_protocol_without_browser(self):
        network = self.network()
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"reply_code":0}'
        network.opener = Mock()
        network.opener.open.return_value = response
        network.login("sample-user", "sample-secret", "default")
        request = network.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "http://m.njust.edu.cn/api/portal/v1/login")
        self.assertEqual(json.loads(request.data), {"username": "sample-user", "password": "sample-secret", "domain": "default"})
        self.assertTrue(request.get_header("Content-type").startswith("application/x-www-form-urlencoded"))

    def test_auth_errors_do_not_echo_portal_personal_data(self):
        network = self.network()
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"reply_code":1,"reply_msg":"sample-secret"}'
        network.opener = Mock()
        network.opener.open.return_value = response
        with self.assertRaises(RuntimeError) as caught:
            network.login("sample-user", "sample-secret", "default")
        self.assertNotIn("sample-secret", str(caught.exception))

    def test_redirects_are_never_followed(self):
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "", {}, "http://other.test"))
        with patch.dict(os.environ, {"HTTP_PROXY": "http://other.test:1234"}):
            self.assertFalse(any(isinstance(handler, urllib.request.ProxyHandler) and handler.proxies for handler in direct_opener().handlers))

    def test_already_online_does_not_reconnect(self):
        network = self.network()
        network.current_ssid = Mock(return_value="NJUST")
        network.online = Mock(return_value=True)
        network.connect = Mock()
        network.login = Mock()
        network.recover({"ssid": "NJUST", "fallback_ssid": "", "domain": "default"}, "u", "p", Mock())
        network.connect.assert_not_called()
        network.login.assert_not_called()

    def test_portal_failure_uses_fallback_and_fallback_returns_to_primary(self):
        for current, online in (("NJUST", [False, False, True]), ("backup", [True, False, True])):
            network = self.network()
            network.current_ssid = Mock(return_value=current)
            network.online = Mock(side_effect=online)
            network.connect = Mock()
            network.login = Mock(side_effect=RuntimeError("校园认证失败"))
            network.recover({"ssid": "NJUST", "fallback_ssid": "backup", "domain": "default"}, "u", "p", Mock())
            self.assertEqual(network.connect.call_args.args, ("backup",))

    def test_stop_cancels_before_authentication_or_wifi_changes(self):
        network = self.network()
        network.cancel.set()
        network.opener = Mock()
        with self.assertRaises(InterruptedError):
            network.login("u", "p", "default")
        network.opener.open.assert_not_called()

    def test_custom_checks_validate_and_apply_timeout(self):
        from njust_wifi.network import parse_checks
        self.assertEqual(parse_checks("https://example.test/check|204|"), [("https://example.test/check", 204, b"")])
        for value in ("file:///private|200|", "http://u:p@example.test/|200|", "http://example.test/|bad|", ""):
            with self.assertRaises(ValueError):
                parse_checks(value)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name))
        self.service = Service(self.store, demo=True)
        self.server = Server(self.service, Path(__file__).resolve().parents[1] / "frontend", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.service.close()
        self.thread.join()
        self.temp.cleanup()

    def call(self, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        options = {"X-NJUST-Token": self.server.token}
        options.update(headers or {})
        if body is not None:
            options["Content-Type"] = "application/json"
        connection.request("POST" if body is not None else "GET", path,
                           json.dumps(body).encode() if body is not None else None, options)
        response = connection.getresponse()
        data = response.read()
        result = (response.status, json.loads(data) if response.getheader("Content-Type", "").startswith("application/json") else data)
        connection.close()
        return result

    def test_rejects_cross_site_origin_host_and_missing_token(self):
        for headers in ({"Origin": "https://other.test"}, {"Host": "other.test"},
                        {"Sec-Fetch-Site": "cross-site"}, {"X-NJUST-Token": ""}):
            with self.subTest(headers=headers):
                self.assertEqual(self.call("/api/config", headers=headers)[0], 403)
        self.assertEqual(self.call("/api/session", headers={"Sec-Fetch-Site": "cross-site"})[0], 403)

    def test_config_and_diagnostics_do_not_disclose_credentials(self):
        self.assertEqual(self.call("/api/config", {"username": "sample-user", "password": "sample-secret", "fallback_ssid": "sample-private-wifi"})[0], 200)
        public = self.call("/api/config")[1]
        self.assertNotIn("password", public)
        self.assertNotIn("username", public)
        diagnostic = json.dumps(self.call("/api/diagnostics")[1])
        for private in ("sample-user", "sample-secret", "sample-private-wifi", str(self.store.root)):
            self.assertNotIn(private, diagnostic)

    def test_explicit_credentials_view_requires_local_token_and_never_exports_secrets(self):
        self.call('/api/config', {'username': 'fixture-account', 'password': 'fixture-password',
            'fallback_networks': [{'ssid': 'private-fallback', 'password': 'wifi-private-pass'}]})
        self.assertEqual(self.call('/api/credentials', headers={'X-NJUST-Token': ''})[0], 403)
        self.assertEqual(self.call('/api/credentials', headers={'Origin': 'https://other.test'})[0], 403)
        self.assertEqual(self.call('/api/credentials')[1], {'username': 'fixture-account', 'password': 'fixture-password'})
        for value in ('fixture-account', 'fixture-password', 'private-fallback', 'wifi-private-pass'):
            self.assertNotIn(value, json.dumps(self.call('/api/diagnostics')[1]))

    def test_start_check_stop_and_forget(self):
        self.assertEqual(self.call("/api/start", {})[0], 200)
        deadline = time.monotonic() + 3
        while (self.service.status()["state"] != "online" or self.service.status()['busy']) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.service.status()["state"], "online")
        self.assertTrue(self.service.status()["running"])
        self.assertIsNotNone(self.service.status()["next_check_at"])
        self.assertEqual(self.call("/api/stop", {})[0], 200)
        self.assertFalse(self.service.status()["running"])
        self.assertEqual(self.call("/api/credentials/forget", {})[0], 200)

    def test_windows_fallback_password_view_is_scoped_transient_and_session_protected(self):
        networks = [{'ssid': 'windows-fixture', 'password': ''}, {'ssid': 'app-fixture', 'password': 'app-secret'}]
        self.store.update({'fallback_networks': networks})
        self.service.demo = False
        before = self.store.settings_path.read_bytes()
        with patch('njust_wifi.service.read_profile_passwords', return_value={'windows-fixture': 'windows-secret', 'unrelated': 'other-secret'}) as reader:
            for headers in ({'X-NJUST-Token': ''}, {'Origin': 'https://other.test'}, {'Host': 'other.test'}):
                self.assertEqual(self.call('/api/config', headers=headers)[0], 403)
            reader.assert_not_called()
            viewed = self.call('/api/config')[1]['fallback_networks']
            reader.assert_called_once_with(['windows-fixture'])
            self.assertEqual(viewed[0]['profile_password'], 'windows-secret')
            self.assertEqual(viewed[0]['password'], '')
            self.assertEqual(viewed[1]['password'], 'app-secret')
            self.assertNotIn('other-secret', json.dumps(viewed))
        self.assertEqual(self.store.settings_path.read_bytes(), before)
        self.assertEqual(self.store.settings['fallback_networks'], networks)
        for path in ('/api/diagnostics', '/api/logs', '/api/status'):
            self.assertNotIn('windows-secret', json.dumps(self.call(path)[1]))

    def test_demo_config_never_reads_windows_wifi_keys(self):
        self.store.update({'fallback_networks': [{'ssid': 'windows-fixture', 'password': ''}]})
        with patch('njust_wifi.service.read_profile_passwords') as reader:
            self.assertEqual(self.call('/api/config')[1]['fallback_networks'][0]['profile_password'], '')
            reader.assert_not_called()

    def test_balance_cache_and_refresh_require_local_session(self):
        self.assertEqual(self.call("/api/balance", headers={"X-NJUST-Token": ""})[0], 403)
        self.assertEqual(self.call("/api/balance/refresh", {}, headers={"Origin": "https://other.test"})[0], 403)
        with patch.object(self.service, "refresh_balance") as refresh:
            self.assertIsNone(self.call("/api/balance")[1]["balance"])
            refresh.assert_not_called()
            self.assertEqual(self.call("/api/balance/refresh", {})[0], 200)
            refresh.assert_called_once_with()

    def test_proxy_controller_configuration_is_session_protected_and_secret_is_write_only(self):
        endpoint='/api/proxy/controller'
        body={'client':'clash-verge','url':'http://127.0.0.1:9097','secret':'fixture-api-secret'}
        self.assertEqual(self.call(endpoint,body,headers={'X-NJUST-Token':''})[0],403)
        self.assertEqual(self.call(endpoint,body)[0],200)
        view=self.call('/api/proxy')[1]['controller']
        self.assertTrue(view['secret_set'])
        for path in ('/api/proxy','/api/diagnostics','/api/logs','/api/status','/api/config'):
            self.assertNotIn('fixture-api-secret',json.dumps(self.call(path)[1]))
        self.assertEqual(self.call(endpoint,{'client':'flclash','url':''})[0],400)
        self.assertEqual(self.call(endpoint,{'url':'http://example.invalid:9090'})[0],400)
        self.assertEqual(self.call('/guide/../proxy-controllers.json')[0],404)

    def test_rejects_unknown_fields_and_paths(self):
        self.assertEqual(self.call("/api/config", {"login_url": "http://other.test"})[0], 400)
        self.assertEqual(self.call("/../app.py")[0], 404)
        self.assertEqual(self.call("/.env.local")[0], 404)

    def test_busy_settings_save_for_next_cycle_and_duplicate_check_is_rejected(self):
        self.service.busy = True
        self.assertEqual(self.call("/api/config", {"interval_minutes": 7})[0], 200)
        self.assertEqual(self.service.store.settings["interval_minutes"], 7)
        self.assertEqual(self.call("/api/check", {})[0], 400)
        self.assertEqual(self.call("/api/credentials/forget", {})[0], 400)
        self.service.busy = False

    def test_program_guard_management_roundtrip_and_privacy(self):
        fixture = self.store.root / "fixtures"
        fixture.mkdir()
        executable = fixture / "sample.exe"
        executable.touch()
        self.assertEqual(self.call("/api/probes/candidates")[1]["candidates"][0]["path"], str(executable))
        self.assertEqual(self.call("/api/probes/add", {"paths": [str(executable)]})[0], 200)
        with patch.object(self.service.guards, "task_exists", return_value=False):
            items = self.call("/api/probes")[1]["probes"]
            self.assertEqual(items[0]["name"], "sample")
            self.assertEqual(self.call("/api/probes/remove", {"ids": [items[0]["id"]]})[0], 200)
            self.assertEqual(self.call("/api/probes")[1]["probes"], [])
            self.call("/api/probes/add", {"paths": [str(executable)]})
            self.assertEqual(self.call("/api/probes/clear", {})[0], 200)
        self.assertNotIn(str(executable), json.dumps(self.call("/api/diagnostics")[1]))

    def test_elevation_is_only_triggered_by_explicit_action(self):
        executable = self.store.root / "sample.exe"
        executable.touch()
        self.service.guards.add([str(executable)])
        ids = [self.service.guards.identifier(executable)]
        self.service.demo = False
        with patch.object(self.service.guards, "task_exists", return_value=False), patch.object(self.service.guards, "configure_task") as setup:
            self.call("/api/probes")
            setup.assert_not_called()
            self.assertEqual(self.call("/api/probes/elevate", {"ids": ids})[0], 200)
            setup.assert_called_once_with(executable)

    def test_log_clear_removes_panel_and_disk_and_reloads_history(self):
        self.service.event("online", "网络连接正常")
        self.assertTrue(self.call("/api/logs")[1]["entries"])
        self.assertEqual(self.call("/api/logs/open", {})[0], 200)
        self.assertEqual(self.call("/api/logs/clear", {})[0], 200)
        self.assertEqual(self.call("/api/logs")[1]["entries"], [])
        self.assertEqual((self.store.root / "events.jsonl").read_text(), "")

    def test_busy_program_edits_do_not_change_configuration(self):
        self.service.busy = True
        self.assertEqual(self.call("/api/probes/add", {"paths": []})[0], 400)
        self.service.busy = False

    def test_healthy_checks_keep_one_log_row_and_refresh_duration(self):
        with patch('njust_wifi.service.time.monotonic', return_value=100):
            self.service.event('online', '网络连接正常')
        with patch('njust_wifi.service.time.monotonic', return_value=90220):
            for _ in range(3):
                self.service.event('checking', '正在检测网络连接')
                self.service.event('online', '网络连接正常')
            logs = self.call('/api/logs')[1]['entries']
            self.assertEqual(len(logs), 1)
            self.assertEqual(logs[0]['duration_seconds'], 90120)
            self.assertTrue(logs[0]['active'])
            self.service._save_logs()
        saved = [json.loads(line) for line in (self.store.root / 'events.jsonl').read_text('utf-8').splitlines()]
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]['duration_seconds'], 90120)

    def test_network_recovery_closes_only_network_span_and_logs_real_operations(self):
        with patch('njust_wifi.service.time.monotonic', return_value=100):
            self.service.health('guard:fixture', '守护程序 1 运行正常')
            self.service.event('online', '网络连接正常')
        with patch('njust_wifi.service.time.monotonic', return_value=160):
            self.service.event('recovering', '检测到网络中断，正在恢复连接')
            self.service.event('recovering', '正在进行校园网认证')
            self.service.event('online', '网络连接正常')
        with patch('njust_wifi.service.time.monotonic', return_value=220):
            logs = self.service.logs()
            self.assertEqual([e['message'] for e in logs], ['守护程序 1 运行正常', '网络连接正常', '检测到网络中断，正在恢复连接', '正在进行校园网认证', '网络连接正常'])
            self.assertEqual(logs[0]['duration_seconds'], 120)
            self.assertEqual(logs[1]['duration_seconds'], 60)
            self.assertFalse(logs[1]['active'])
            self.assertEqual(logs[-1]['duration_seconds'], 60)
            self.service.stop()
            self.assertFalse(any(e.get('active') for e in self.service.logs()))

    def test_guard_restart_does_not_reset_network_duration(self):
        with patch('njust_wifi.service.time.monotonic', return_value=100):
            self.service.event('online', '网络连接正常')
            self.service.health('guard:fixture', '守护程序 1 运行正常')
        with patch('njust_wifi.service.time.monotonic', return_value=160):
            self.service.health('guard:fixture', '', False)
            self.service.guard_event('idle', '守护程序 1 未运行，正在启动')
            self.service.health('guard:fixture', '守护程序 1 运行正常')
        with patch('njust_wifi.service.time.monotonic', return_value=220):
            logs = self.service.logs()
            self.assertEqual(logs[0]['duration_seconds'], 120)
            self.assertEqual(logs[1]['duration_seconds'], 60)
            self.assertEqual(logs[-1]['duration_seconds'], 60)
            self.assertEqual(self.service.status()['state'], 'online')

    def test_clear_logs_discards_active_rows_and_next_healthy_check_starts_fresh(self):
        self.service.event('online', '网络连接正常')
        self.service.health('guard:fixture', '守护程序 1 运行正常')
        self.service.clear_logs()
        self.assertEqual(self.service.logs(), [])
        self.assertEqual(self.service.health_spans, {})
        self.service.event('online', '网络连接正常')
        self.assertEqual(len(self.service.logs()), 1)
        self.assertEqual(self.service.logs()[0]['duration_seconds'], 0)

    def test_repeated_worker_checks_do_not_append_healthy_logs(self):
        for _ in range(3):
            self.service.start(once=True)
            deadline = time.monotonic() + 2
            while self.service.once or self.service.busy:
                if time.monotonic() > deadline:
                    self.fail('worker did not finish')
                time.sleep(.01)
        self.assertEqual(len(self.service.logs()), 1)
        self.assertNotIn('正在检测', json.dumps(self.service.logs(), ensure_ascii=False))

    def test_reopened_log_freezes_last_checkpoint_instead_of_counting_downtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry = {'time': '2026-11-09T00:00:00+00:00', 'level': 'info', 'message': '网络连接正常', 'duration_seconds': 172861, 'active': True}
            (root / 'events.jsonl').write_text(json.dumps(entry) + '\n', encoding='utf-8')
            reopened = Service(Store(root), demo=True)
            try:
                self.assertFalse(reopened.logs()[0]['active'])
                self.assertEqual(reopened.logs()[0]['duration_seconds'], 172861)
            finally:
                reopened.close()

    def test_legacy_healthy_check_noise_is_compacted_without_removing_recovery_actions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            records = [
                {'time': '2026-11-09T00:00:00Z', 'level': 'info', 'message': '网络连接正常'},
                {'time': '2026-11-09T00:05:00Z', 'level': 'info', 'message': '正在检测网络连接'},
                {'time': '2026-11-09T00:05:00Z', 'level': 'info', 'message': '网络连接正常'},
                {'time': '2026-11-09T00:06:00Z', 'level': 'info', 'message': '检测到网络中断，正在恢复连接'}]
            (root / 'events.jsonl').write_text(''.join(json.dumps(e) + '\n' for e in records), 'utf-8')
            reopened = Service(Store(root), demo=True)
            try:
                self.assertEqual(len(reopened.logs()), 2)
                self.assertEqual(reopened.logs()[0]['duration_seconds'], 300)
                self.assertEqual(reopened.logs()[1]['message'], records[-1]['message'])
            finally:
                reopened.close()


if __name__ == "__main__":
    unittest.main()
