import json
import threading
import time
import unittest
from unittest.mock import Mock, patch

from njust_wifi import proxy as proxy_module
from njust_wifi.client_control import ClientControlError
from njust_wifi.proxy import ControllerError, ProxyProtection
from njust_wifi.service import Service

import test_proxy


class NodeSelectionTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.group = '线路 / 香港%'
        self.rows = {self.group: {'type': 'Selector', 'all': ['Node A', 'Node B', 'Auto'], 'now': 'Node A'},
                     'GLOBAL': {'type': 'Selector', 'all': [self.group, 'DIRECT'], 'now': self.group},
                     'Auto': {'type': 'URLTest', 'all': ['Node A', 'Node B'], 'now': 'Node A'},
                     'Node A': {'type': 'VLESS', 'history': [{'delay': 44}], 'password': 'private'},
                     'Node B': {'type': 'SS', 'history': [{'delay': 0}]}, 'DIRECT': {'type': 'Direct'}}
        self.controller = Mock()
        self.controller.request.side_effect = self.request
        self.proxy = ProxyProtection(self.root, controller=self.controller)

    def request(self, method, path, payload=None):
        import urllib.parse
        if method == 'GET' and path == '/proxies':
            return {'proxies': self.rows}
        if method == 'PUT':
            name = urllib.parse.unquote(path.removeprefix('/proxies/'))
            self.rows[name]['now'] = payload['name']
            return {}
        self.fail('Unexpected controller operation: ' + method + ' ' + path)

    def selection(self, **overrides):
        return {'client': 'clash-verge', 'group': self.group, 'node': 'Node B'} | overrides

    def test_exposes_existing_delays_groups_without_credentials_or_active_tests(self):
        data = self.proxy.node_choices()
        self.assertIn('GLOBAL', [g['name'] for g in data['groups']])
        self.assertEqual(data['nodes']['Node A']['delay'], 44)
        self.assertIsNone(data['nodes']['Node B']['delay'])
        self.assertFalse(next(g for g in data['groups'] if g['name'] == 'Auto')['selectable'])
        self.assertNotIn('private', json.dumps(data))
        self.controller.request.assert_called_once_with('GET', '/proxies')

    def test_switches_encoded_group_and_confirms_core_without_touching_settings(self):
        import urllib.parse
        self.proxy.group = 'guard-group'
        self.proxy.policies = {'keep': 'direct'}
        self.proxy.exit_state = 'healthy'; self.proxy.recoveries = 3; self.proxy.recovery_exit = 'old'
        self.proxy.recovery_path.write_text('preserve-original-switches')
        self.proxy.select_node(self.selection())
        self.assertEqual(self.rows[self.group]['now'], 'Node B')
        self.controller.request.assert_any_call('PUT', '/proxies/' + urllib.parse.quote(self.group, safe=''), {'name': 'Node B'})
        self.assertEqual(self.proxy.group, 'guard-group')
        self.assertEqual(self.proxy.policies, {'keep': 'direct'})
        self.assertEqual(self.proxy.recovery_path.read_text(), 'preserve-original-switches')
        self.assertEqual((self.proxy.exit_state, self.proxy.recoveries, self.proxy.recovery_exit), ('uncertain', 0, ''))
        self.assertFalse(self.proxy.path.exists())

    def test_rejects_wrong_client_automatic_group_and_stale_node_without_write(self):
        for raw in (self.selection(client='flclash'), self.selection(group='Auto'), self.selection(node='Removed'),
                    self.selection(node=2), self.selection(extra='ignored')):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                self.proxy.select_node(raw)
        self.assertFalse(any(c.args[0] == 'PUT' for c in self.controller.request.call_args_list))

    def test_unconfirmed_write_never_reports_success_or_keeps_old_health(self):
        self.controller.request.side_effect = lambda method, path, payload=None: {'proxies': self.rows} if method == 'GET' else {}
        self.proxy.exit_state = 'healthy'
        with self.assertRaisesRegex(ValueError, '未确认'):
            self.proxy.select_node(self.selection())
        self.assertEqual(self.proxy.exit_state, 'uncertain')

    def test_same_selection_is_a_noop_and_flclash_uses_its_current_controller(self):
        self.proxy.select_node(self.selection(node='Node A'))
        self.assertFalse(any(c.args[0] == 'PUT' for c in self.controller.request.call_args_list))
        self.proxy.client = 'flclash'
        self.proxy.select_node(self.selection(client='flclash'))
        self.assertEqual(self.rows[self.group]['now'], 'Node B')

    def test_queued_check_never_uses_a_different_client_after_lock_acquisition(self):
        event = Mock()
        self.proxy.tick(threading.Event(), event, expected_client='flclash', required_failures=1)
        self.controller.request.assert_not_called()
        event.assert_not_called()


class ManualCheckJobTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        from njust_wifi.storage import Store
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = Service(Store(Path(self.temp.name)), demo=True)
        self.started = threading.Event(); self.release = threading.Event()
        self.addCleanup(self.service.close)
        self.addCleanup(self.release.set)

    def wait_done(self):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            state = self.service.proxy_check_status()
            if state['state'] in ('done', 'cancelled'):
                return state
            time.sleep(.01)
        self.fail('Manual proxy check did not complete')

    def blocked_tick(self, *args, **kwargs):
        self.started.set(); self.release.wait(3)
        self.service.proxy.exit_state = 'healthy'
        self.service.proxy.message = '代理出口正常'

    def test_coalesces_requests_and_publishes_only_final_result(self):
        with patch.object(self.service.proxy, 'tick', side_effect=self.blocked_tick):
            first = self.service.proxy_action('check', {})
            self.assertTrue(self.started.wait(2))
            running = self.service.proxy_check_status()
            self.assertEqual(running['state'], 'running'); self.assertIsNone(running['result'])
            self.assertEqual(first, self.service.proxy_action('check', {}))
            self.release.set()
            done = self.wait_done()
            self.assertEqual(done['id'], first['check_id'])
            self.assertEqual(done['result']['exit_state'], 'healthy')

    def test_switching_client_cancels_old_result(self):
        with patch.object(self.service.proxy, 'tick', side_effect=self.blocked_tick):
            self.service.proxy_action('check', {})
            self.assertTrue(self.started.wait(2))
            self.service.proxy_action('client', {'client': 'flclash'})
            state = self.service.proxy_check_status()
            self.assertEqual(state['state'], 'cancelled'); self.assertIsNone(state['result'])
            self.release.set()

    def test_unexpected_failure_finishes_uncertain_and_keeps_worker_alive(self):
        with patch.object(self.service.proxy, 'tick', side_effect=RuntimeError('private-payload')):
            self.service.proxy_action('check', {})
            result = self.wait_done()
            self.assertEqual(result['state'], 'done')
            self.assertEqual(result['result']['exit_state'], 'uncertain')
            self.assertNotIn('private-payload', json.dumps(result))
            self.assertTrue(self.service.proxy_worker.is_alive())


class GuardFixture(unittest.TestCase):
    setUp = test_proxy.ProxyTests.setUp
    write_client_flags = test_proxy.ProxyTests.write_client_flags

    def exit_fixture(self):
        """A routable exit whose health the test controls; switches stay real fixtures."""
        self.exit_dead = True
        original = self.controller.request.side_effect

        def request(method, path, payload=None):
            if method == 'GET' and path == '/configs':
                return self.config | {'mode': 'rule'}
            if path == '/rules':
                return {'rules': [{'type': 'Match', 'proxy': 'Main'}]}
            if path == '/proxies':
                return {'proxies': {'Main': {'type': 'Selector', 'all': ['Node'], 'now': 'Node'},
                                    'Node': {'type': 'VLESS'}, 'DIRECT': {'type': 'Direct'}}}
            if '/delay?' in path:
                if self.exit_dead:
                    raise ControllerError(504)
                return {'delay': 10}
            return original(method, path, payload)
        self.controller.request.side_effect = request
        self.proxy.direct_healthy = Mock(return_value=True)

    def tick(self, event=None, *, automatic=True, rounds=1):
        event = event or Mock()
        for _ in range(rounds):
            self.proxy.tick(threading.Event(), event, required_failures=1, auto_restore=lambda: automatic)
        return event

    def close_proxy(self):
        self.exit_dead = True
        event = self.tick()
        self.assertTrue(self.proxy.recovery_path.exists())
        self.assertEqual(self.gui, {'proxy': False, 'tun': False})
        return event


class AutoRestoreTests(GuardFixture):
    def setUp(self):
        super().setUp()
        self.exit_fixture()
        self.proxy.set_options({'auto_restore': True})

    def test_closed_proxy_still_waits_for_the_user_by_default(self):
        self.close_proxy()
        self.exit_dead = False
        self.switches.set.reset_mock()
        self.tick(automatic=False, rounds=8)
        self.switches.set.assert_not_called()
        self.assertEqual((self.proxy.state, self.proxy.message), ('direct', '代理出口正常，等待手动恢复代理'))
        self.assertEqual(self.proxy.status()['exit_state'], 'healthy')
        self.assertTrue(self.proxy.recovery_path.exists())

    def test_restores_only_after_the_exit_stays_healthy(self):
        self.close_proxy()
        self.exit_dead = False
        event = self.tick(rounds=proxy_module.RECOVERY_ROUNDS - 1)
        self.assertEqual(self.gui, {'proxy': False, 'tun': False})
        self.assertEqual(self.proxy.status()['recovering'], {'count': 3, 'needed': 4})
        event.assert_not_called()
        self.tick(event)
        self.assertEqual(self.gui, {'proxy': True, 'tun': True})
        self.assertEqual(self.registry['ProxyEnable'], 1)
        self.assertFalse(self.proxy.recovery_path.exists())
        self.assertEqual(self.proxy.state, 'restored')
        event.assert_called_once()
        self.assertIn('已自动恢复', event.call_args.args[1])

    def test_one_bad_round_restarts_the_confirmation(self):
        self.close_proxy()
        self.exit_dead = False
        self.tick(rounds=3)
        self.exit_dead = True
        self.tick()
        self.assertEqual(self.proxy.recoveries, 0)
        self.exit_dead = False
        self.tick(rounds=3)
        self.assertEqual(self.gui, {'proxy': False, 'tun': False})

    def test_failing_again_soon_doubles_the_wait_and_then_stops(self):
        for flaps in range(proxy_module.MAX_FLAPS):
            self.close_proxy()
            self.assertEqual(self.proxy.flaps, flaps)
            self.exit_dead = False
            needed = proxy_module.RECOVERY_ROUNDS * 2 ** flaps
            self.tick(rounds=needed - 1)
            self.assertEqual(self.gui, {'proxy': False, 'tun': False})
            self.tick()
            self.assertEqual(self.gui, {'proxy': True, 'tun': True})
        self.close_proxy()
        self.assertEqual(self.proxy.flaps, proxy_module.MAX_FLAPS)
        self.exit_dead = False
        self.switches.set.reset_mock()
        self.tick(rounds=40)
        self.switches.set.assert_not_called()
        self.assertIn('已停止自动恢复', self.proxy.message)
        self.assertTrue(self.proxy.status()['auto_restore_paused'])
        # The button still works, and using it gives automation a clean slate.
        self.proxy.restore()
        self.assertEqual(self.gui, {'proxy': True, 'tun': True})
        self.assertEqual(self.proxy.flaps, 0)

    def test_an_exit_that_holds_for_the_window_forgets_earlier_flaps(self):
        self.close_proxy()
        self.exit_dead = False
        self.tick(rounds=proxy_module.RECOVERY_ROUNDS)
        self.close_proxy()
        self.assertEqual(self.proxy.flaps, 1)
        self.exit_dead = False
        self.tick(rounds=proxy_module.RECOVERY_ROUNDS * 2)
        self.assertEqual(self.gui, {'proxy': True, 'tun': True})
        self.proxy.restored_at = time.monotonic() - proxy_module.FLAP_WINDOW - 1
        self.tick()
        self.assertEqual((self.proxy.flaps, self.proxy.restored_at), (0, None))

    def test_unfinished_automatic_restores_stop_after_a_few_attempts(self):
        self.close_proxy()
        self.exit_dead = False
        self.switches.read.reset_mock()
        self.switches.read.side_effect = ClientControlError('未找到代理客户端窗口，请保持客户端运行')
        self.switches.set.reset_mock()
        self.tick(rounds=proxy_module.RECOVERY_ROUNDS * (proxy_module.MAX_AUTO_ATTEMPTS + 3))
        self.assertEqual(self.proxy.auto_attempts, proxy_module.MAX_AUTO_ATTEMPTS)
        self.assertEqual(self.switches.read.call_count, proxy_module.MAX_AUTO_ATTEMPTS)
        self.switches.set.assert_not_called()
        self.assertEqual(self.proxy.message, '自动恢复未完成，请点击恢复代理')
        self.assertTrue(self.proxy.recovery_path.exists())

    def test_a_restore_done_in_the_client_is_respected(self):
        self.close_proxy()
        self.gui.update(proxy=True, tun=True)
        self.config['tun']['enable'] = True
        self.registry['ProxyEnable'] = 1
        self.write_client_flags()
        self.exit_dead = False
        self.switches.set.reset_mock()
        self.tick()
        self.switches.set.assert_not_called()
        self.assertFalse(self.proxy.recovery_path.exists())
        self.assertEqual(self.proxy.message, '客户端已手动恢复，继续检测出口')

    def test_status_reports_what_was_closed_without_leaking_the_proxy_address(self):
        self.assertEqual(self.proxy.status()['closed'], [])
        self.close_proxy()
        status = self.proxy.status()
        self.assertEqual((status['closed'], status['cause']), (['tun', 'proxy'], 'exit'))
        self.assertTrue(status['closed_at'] and status['checked_at'])
        self.assertTrue(status['restore_available'])
        self.assertIs(status['connected'], True)
        self.assertNotIn('7897', json.dumps(status))


class OptionTests(GuardFixture):
    def test_options_default_off_persist_and_reject_other_input(self):
        self.assertEqual((self.proxy.auto_restore, self.proxy.clear_stale), (False, False))
        self.proxy.set_options({'auto_restore': True})
        self.proxy.set_options({'clear_stale': True})
        reopened = ProxyProtection(self.root, controller=self.controller, switches=self.switches)
        self.assertEqual((reopened.auto_restore, reopened.clear_stale), (True, True))
        self.assertEqual((reopened.status()['auto_restore'], reopened.status()['clear_stale']), (True, True))
        for raw in ({}, {'auto_restore': 1}, {'clear_stale': 'yes'}, {'other': True}, [], None):
            with self.assertRaises(ValueError):
                self.proxy.set_options(raw)
        self.assertEqual((self.proxy.auto_restore, self.proxy.clear_stale), (True, True))

    def test_options_survive_a_client_switch_and_an_older_settings_file(self):
        self.proxy.set_options({'auto_restore': True})
        self.proxy.set_client('flclash')
        self.proxy.set_client('clash-verge')
        self.assertTrue(json.loads(self.proxy.path.read_text('utf-8'))['options']['auto_restore'])
        self.proxy.path.write_text(json.dumps({'client': 'clash-verge', 'group': '', 'policies': {}, 'managed_rules': []}))
        reopened = ProxyProtection(self.root, controller=self.controller, switches=self.switches)
        self.assertEqual((reopened.auto_restore, reopened.clear_stale), (False, False))

    def test_quiet_connect_reports_groups_or_a_reason_without_raising(self):
        self.assertEqual(self.proxy.connect(), {'connected': True, 'groups': ['Sample']})
        self.assertIs(self.proxy.status()['connected'], True)
        self.controller.request.side_effect = OSError('fixture: client is not running')
        reply = self.proxy.connect()
        self.assertEqual((reply['connected'], reply['groups']), (False, []))
        self.assertTrue(reply['message'])
        self.assertIs(self.proxy.status()['connected'], False)


class IndependentExitTests(GuardFixture):
    def setUp(self):
        super().setUp()
        self.exit_fixture()

    def test_manual_mode_keeps_probing_closed_switches_and_reports_failures(self):
        self.close_proxy()
        self.switches.set.reset_mock()
        self.exit_dead = False
        self.tick(automatic=False, rounds=3)
        self.assertEqual(self.proxy.status()['exit_state'], 'healthy')
        self.assertEqual(self.proxy.recoveries, 0)
        self.exit_dead = True
        self.tick(automatic=False)
        self.assertEqual(self.proxy.status()['exit_state'], 'failed')
        self.assertIn('代理出口异常', self.proxy.message)
        self.assertEqual(self.gui, {'proxy': False, 'tun': False})
        self.switches.set.assert_not_called()

    def test_turning_auto_restore_off_mid_confirmation_keeps_monitoring(self):
        self.close_proxy()
        self.proxy.set_options({'auto_restore': True})
        self.exit_dead = False
        for _ in range(proxy_module.RECOVERY_ROUNDS - 1):
            self.proxy.tick(threading.Event(), Mock(), auto_restore=lambda: self.proxy.auto_restore)
        self.proxy.set_options({'auto_restore': False})
        self.switches.set.reset_mock()
        for _ in range(proxy_module.RECOVERY_ROUNDS + 1):
            self.proxy.tick(threading.Event(), Mock(), auto_restore=lambda: self.proxy.auto_restore)
        self.assertEqual(self.proxy.status()['exit_state'], 'healthy')
        self.assertEqual(self.gui, {'proxy': False, 'tun': False})
        self.assertEqual(self.proxy.recoveries, 0)
        self.switches.set.assert_not_called()
        self.proxy.set_options({'auto_restore': True})
        for _ in range(proxy_module.RECOVERY_ROUNDS):
            self.proxy.tick(threading.Event(), Mock(), auto_restore=lambda: self.proxy.auto_restore)
        self.assertEqual(self.gui, {'proxy': True, 'tun': True})

    def test_unknown_controller_result_is_not_bad_or_healthy(self):
        self.close_proxy()
        original = self.controller.request.side_effect
        def unauthorized_probe(method, path, payload=None):
            if '/delay?' in path:
                raise ControllerError(401)
            return original(method, path, payload)
        self.controller.request.side_effect = unauthorized_probe
        self.switches.set.reset_mock()
        self.tick(automatic=False)
        self.assertEqual(self.proxy.status()['exit_state'], 'uncertain')
        self.assertIn('待确认', self.proxy.message)
        self.switches.set.assert_not_called()

    def test_controller_failure_before_probe_never_reuses_an_old_healthy_result(self):
        self.close_proxy()
        self.proxy.exit_state = 'healthy'
        self.controller.request.side_effect = ControllerError(401)
        self.switches.set.reset_mock()
        self.tick(automatic=False)
        self.assertEqual(self.proxy.status()['exit_state'], 'uncertain')
        self.switches.set.assert_not_called()

    def test_changing_controller_invalidates_health_and_confirmation(self):
        self.proxy.exit_state = 'healthy'
        self.proxy.recoveries = 3
        self.proxy.recovery_exit = 'Main → Old'
        self.proxy.configure_controller({'url': 'http://127.0.0.1:9097', 'secret': 'fixture-key'})
        self.assertEqual(self.proxy.exit_state, '')
        self.assertEqual((self.proxy.recoveries, self.proxy.recovery_exit), (0, ''))

    def test_waiting_for_a_stopped_client_invalidates_old_exit_health(self):
        self.proxy.exit_state = 'healthy'
        with patch.object(self.proxy, '_stale', return_value=(True, None)):
            self.assertTrue(self.proxy.stale_tick(threading.Event(), Mock()))
        self.assertEqual(self.proxy.exit_state, 'uncertain')
        self.switches.set.assert_not_called()

    def test_switches_disabled_by_user_are_never_turned_on_by_monitoring(self):
        self.gui.update(proxy=False, tun=False)
        self.registry['ProxyEnable'] = 0
        self.config['tun']['enable'] = False
        self.write_client_flags()
        self.exit_dead = False
        self.tick(rounds=proxy_module.RECOVERY_ROUNDS + 1)
        self.assertEqual(self.proxy.status()['exit_state'], 'healthy')
        self.assertFalse(self.proxy.recovery_path.exists())
        self.switches.set.assert_not_called()

    def test_automatic_restore_only_restores_the_previously_enabled_switch(self):
        self.gui['proxy'] = False
        self.registry['ProxyEnable'] = 0
        self.write_client_flags()
        self.close_proxy()
        self.exit_dead = False
        self.switches.set.reset_mock()
        self.tick(rounds=proxy_module.RECOVERY_ROUNDS)
        self.assertEqual(self.gui, {'proxy': False, 'tun': True})
        self.assertEqual([call.args[0] for call in self.switches.set.call_args_list], ['tun'])

    def test_stability_counter_follows_one_selected_exit(self):
        self.close_proxy()
        self.exit_dead = False
        self.tick(rounds=proxy_module.RECOVERY_ROUNDS - 1)
        self.assertEqual(self.proxy.recoveries, 3)
        with patch.object(self.proxy, 'active_exit', side_effect=self.second_exit):
            self.tick()
            self.assertEqual(self.proxy.recoveries, 1)
            self.assertEqual(self.gui, {'proxy': False, 'tun': False})
            self.tick(rounds=proxy_module.RECOVERY_ROUNDS - 1)
            self.assertEqual(self.gui, {'proxy': True, 'tun': True})

    def second_exit(self):
        self.proxy.egress = 'Main → Second'
        return 'Second'

    def test_selection_change_during_last_probe_prevents_restore(self):
        self.close_proxy()
        self.exit_dead = False
        self.tick(rounds=proxy_module.RECOVERY_ROUNDS - 1)
        self.switches.set.reset_mock()
        original = self.proxy.active_exit
        count = 0
        def switching():
            nonlocal count
            count += 1
            return original() if count == 1 else self.second_exit()
        with patch.object(self.proxy, 'active_exit', side_effect=switching):
            self.tick()
        self.assertEqual(self.proxy.recoveries, 0)
        self.assertEqual(self.proxy.exit_state, 'uncertain')
        self.switches.set.assert_not_called()

    def test_paused_restoration_still_updates_exit_state(self):
        self.close_proxy()
        self.proxy.flaps = proxy_module.MAX_FLAPS
        self.exit_dead = False
        self.switches.set.reset_mock()
        self.tick()
        self.assertEqual(self.proxy.status()['exit_state'], 'healthy')
        self.assertTrue(self.proxy.status()['auto_restore_paused'])
        self.exit_dead = True
        self.tick()
        self.assertEqual(self.proxy.status()['exit_state'], 'failed')
        self.switches.set.assert_not_called()

    def test_bad_delay_values_never_report_a_healthy_exit(self):
        original = self.controller.request.side_effect
        for value in (True, 0, -1, '12', None, float('nan'), float('inf'), 3501):
            with self.subTest(value=value):
                def reply(method, path, payload=None):
                    return {'delay': value} if '/delay?' in path else original(method, path, payload)
                self.controller.request.side_effect = reply
                self.assertIsNone(self.proxy._probe_exit())
                self.assertEqual(self.proxy.exit_state, 'uncertain')

    def test_worker_observes_pending_recovery_with_both_options_off(self):
        worker = Service.__new__(Service)
        worker.lock = threading.RLock()
        worker.proxy_check = {'id': None, 'state': 'idle', 'client': 'clash-verge', 'result': None}
        worker.closed = threading.Event()
        worker.demo = False
        worker.store = Mock(settings={'proxy_fail_open': False})
        worker.proxy = Mock(auto_restore=False, clear_stale=False, policies={}, managed_rules=[])
        worker.proxy.recovery_path.exists.return_value = True
        worker.proxy.stale_path.exists.return_value = False
        worker.proxy_check_requested = threading.Event()
        worker.guard_event = Mock()
        worker.observe_proxy = Mock()
        worker.proxy_wake = Mock()
        worker.proxy_wake.wait.side_effect = lambda timeout: worker.closed.set()
        allowed = []
        def tick(cancel, event, can_check, **options):
            allowed.append((can_check(), options['switch_allowed'](), options['auto_restore']()))
        worker.proxy.tick.side_effect = tick
        worker._proxy_work()
        self.assertEqual(allowed, [(True, False, False)])
        worker.proxy_wake.wait.assert_called_once_with(15)

    def test_health_log_tracks_node_reachability_while_switches_remain_closed(self):
        worker = Service.__new__(Service)
        worker.proxy = self.proxy
        worker.health = Mock()
        worker.lock = threading.RLock()
        worker.maintenance_states = {}
        worker.guard_event = Mock()
        self.close_proxy()
        self.exit_dead = False
        self.tick(automatic=False)
        worker.observe_proxy()
        worker.health.assert_called_once_with('proxy', '代理出口正常', True)
        self.assertEqual(self.gui, {'proxy': False, 'tun': False})


class StaleProxyTests(GuardFixture):
    def setUp(self):
        super().setUp()
        self.listening = False
        self.proxy._port_listening = Mock(side_effect=lambda port: self.listening)
        self.proxy.direct_healthy = Mock(return_value=True)
        self.proxy.write_system_proxy = Mock(side_effect=lambda enabled: self.registry.__setitem__('ProxyEnable', int(enabled)))
        self.event = Mock()

    def stale(self, rounds=1, enabled=True):
        result = None
        for _ in range(rounds):
            result = self.proxy.stale_tick(threading.Event(), self.event, lambda: enabled)
        return result

    def test_refused_port_is_closed_only_after_repeated_rounds_and_a_working_direct_path(self):
        self.assertTrue(self.stale(rounds=proxy_module.STALE_ROUNDS - 1))
        self.proxy.write_system_proxy.assert_not_called()
        self.assertEqual(self.proxy.state, 'checking')
        self.assertTrue(self.stale())
        self.proxy.write_system_proxy.assert_called_once_with(False)
        self.assertEqual(self.registry, {'ProxyEnable': 0, 'ProxyServer': '127.0.0.1:7897'})
        self.event.assert_called_once()
        status = self.proxy.status()
        self.assertEqual((status['state'], status['closed'], status['cause']), ('direct', ['proxy'], 'stale'))
        self.assertFalse(status['restore_available'])
        self.switches.set.assert_not_called()
        self.switches.read.assert_not_called()

    def test_anything_short_of_a_proven_dead_own_port_is_left_alone(self):
        cases = {
            'a listening port': lambda: setattr(self, 'listening', True),
            'an unknown answer': lambda: setattr(self, 'listening', None),
            'another client port': lambda: self.registry.update(ProxyServer='127.0.0.1:9999'),
            'a remote proxy': lambda: self.registry.update(ProxyServer='proxy.example.test:8080'),
            'mixed servers': lambda: self.registry.update(ProxyServer='http=127.0.0.1:7897;https=10.0.0.2:8080'),
            'a disabled proxy': lambda: self.registry.update(ProxyEnable=0),
            'an unreadable client config': lambda: setattr(self.controller.runtime, 'side_effect', OSError('missing')),
            'a failing direct path': lambda: setattr(self.proxy.direct_healthy, 'return_value', False),
            'the option being off': None,
        }
        for name, arrange in cases.items():
            with self.subTest(name):
                self.listening = False
                self.registry.update(ProxyEnable=1, ProxyServer='127.0.0.1:7897')
                self.controller.runtime.side_effect = None
                self.proxy.direct_healthy.return_value = True
                self.proxy.stale_failures = 0
                if arrange:
                    arrange()
                self.stale(rounds=proxy_module.STALE_ROUNDS + 2, enabled=arrange is not None)
                self.proxy.write_system_proxy.assert_not_called()
                self.assertEqual(self.registry['ProxyEnable'], 0 if name == 'a disabled proxy' else 1)
                self.assertFalse(self.proxy.stale_path.exists())

    def test_a_change_during_the_last_check_cancels_the_close(self):
        answers = iter([False, False, False, True])
        self.proxy._port_listening = Mock(side_effect=lambda port: next(answers))
        self.stale(rounds=proxy_module.STALE_ROUNDS)
        self.proxy.write_system_proxy.assert_not_called()
        self.assertFalse(self.proxy.stale_path.exists())

    def test_record_retires_when_the_client_sets_its_proxy_again(self):
        self.stale(rounds=proxy_module.STALE_ROUNDS)
        self.proxy.write_system_proxy.reset_mock()
        self.registry['ProxyEnable'] = 1
        self.listening = True
        self.assertFalse(self.stale())
        self.assertFalse(self.proxy.stale_path.exists())
        self.proxy.write_system_proxy.assert_not_called()

    def test_returning_client_keeps_the_proxy_off_unless_automatic_restore_is_on(self):
        self.stale(rounds=proxy_module.STALE_ROUNDS)
        self.proxy.write_system_proxy.reset_mock()
        self.listening = True
        self.assertFalse(self.stale())
        self.proxy.write_system_proxy.assert_not_called()
        self.assertEqual(self.registry['ProxyEnable'], 0)
        self.assertFalse(self.proxy.stale_path.exists())

    def test_automatic_restore_reenables_only_what_the_client_still_wants(self):
        self.proxy.set_options({'auto_restore': True})
        self.listening = False
        self.stale(rounds=proxy_module.STALE_ROUNDS)
        self.proxy.write_system_proxy.reset_mock()
        self.listening = True
        self.assertTrue(self.stale(rounds=proxy_module.STALE_RECOVERY_ROUNDS - 1))
        self.proxy.write_system_proxy.assert_not_called()
        self.assertFalse(self.stale())
        self.proxy.write_system_proxy.assert_called_once_with(True)
        self.assertEqual(self.registry['ProxyEnable'], 1)
        self.assertFalse(self.proxy.stale_path.exists())

        # The client itself now says its system proxy is off: leave it off.
        self.listening = False
        self.proxy.stale_clears = []
        self.stale(rounds=proxy_module.STALE_ROUNDS)
        self.gui['proxy'] = False
        self.write_client_flags()
        self.proxy.write_system_proxy.reset_mock()
        self.listening = True
        self.stale(rounds=3)
        self.proxy.write_system_proxy.assert_not_called()
        self.assertEqual(self.registry['ProxyEnable'], 0)

    def test_stops_when_something_keeps_reenabling_a_dead_proxy(self):
        for _ in range(3):
            self.stale(rounds=proxy_module.STALE_ROUNDS)
            self.assertEqual(self.registry['ProxyEnable'], 0)
            self.registry['ProxyEnable'] = 1
            self.stale()  # the record retires because the proxy is set again
        self.proxy.write_system_proxy.reset_mock()
        self.assertTrue(self.stale(rounds=proxy_module.STALE_ROUNDS + 2))
        self.proxy.write_system_proxy.assert_not_called()
        self.assertEqual(self.proxy.state, 'error')

    def test_an_unconfirmed_write_leaves_no_record(self):
        self.proxy.write_system_proxy = Mock()
        self.stale(rounds=proxy_module.STALE_ROUNDS)
        self.assertFalse(self.proxy.stale_path.exists())
        self.assertEqual(self.proxy.state, 'error')

    def test_never_runs_in_demo_or_while_a_client_switch_recovery_is_pending(self):
        demo = ProxyProtection(self.root / 'demo', demo=True, controller=self.controller, switches=self.switches)
        demo.system_proxy = Mock(side_effect=AssertionError('demo must not read the registry'))
        self.assertFalse(demo.stale_tick(threading.Event(), self.event))
        self.proxy.fail_open()
        self.registry.update(ProxyEnable=1)
        self.assertFalse(self.stale(rounds=proxy_module.STALE_ROUNDS + 1))
        self.proxy.write_system_proxy.assert_not_called()

    def test_restore_button_explains_a_stale_record(self):
        self.stale(rounds=proxy_module.STALE_ROUNDS)
        with self.assertRaisesRegex(ValueError, '尚未运行'):
            self.proxy.restore()
        self.switches.set.assert_not_called()

    def test_closed_loopback_port_is_reported_as_refused(self):
        import socket
        probe = socket.socket()
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
        with probe:
            probe.listen(1)
            self.assertIs(ProxyProtection._port_listening(port), True)
        self.assertIs(ProxyProtection._port_listening(port), False)

    def test_registry_write_is_never_reached_through_the_client_switch_path(self):
        with patch.object(ProxyProtection, 'write_system_proxy') as write:
            self.proxy.fail_open()
            self.proxy.restore()
            write.assert_not_called()


if __name__ == '__main__':
    unittest.main()
