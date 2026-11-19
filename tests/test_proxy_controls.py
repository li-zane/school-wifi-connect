"""Proxy controls use isolated files, fake switches and a fake local core only."""
from copy import deepcopy
from contextlib import closing
import itertools
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
import urllib.parse

import pytest

from njust_wifi.client_control import ClientControlError, ClientSwitches
from njust_wifi.proxy import ControllerError, ProxyProtection, SwitchStateError, default_fallback
from njust_wifi.service import Service
from njust_wifi.subscription import SubscriptionReader
import test_app
import test_proxy_guard


def settings(order=None, **enabled):
    value = default_fallback()
    value['order'] = list(order or value['order'])
    value['enabled'].update(enabled)
    return value


@pytest.mark.parametrize('raw', [None, [], {}, {'order': []},
    {'order': ['switch_node'] * 3, 'enabled': default_fallback()['enabled']},
    {'order': [{}, 'close_tun', 'close_proxy'], 'enabled': default_fallback()['enabled']},
    {'order': list(default_fallback()['order']), 'enabled': []},
    settings(switch_node=1), settings(close_tun='true'),
    default_fallback() | {'extra': True},
    {'order': tuple(default_fallback()['order']), 'enabled': default_fallback()['enabled']},
    {'order': list(default_fallback()['order']), 'enabled': {'switch_node': False}}])
def test_invalid_fallback_is_rejected_and_disk_falls_back(tmp_path, raw):
    proxy = ProxyProtection(tmp_path, demo=True)
    with pytest.raises(ValueError, match='故障处置设置格式不正确'):
        proxy.set_fallback(raw)
    assert not proxy.path.exists()
    proxy.path.write_text(json.dumps({'options': {'fallback': raw}}), encoding='utf-8')
    assert ProxyProtection(tmp_path, demo=True).fallback == default_fallback()


def test_fallback_persists_globally_and_returns_independent_copies(tmp_path):
    proxy = ProxyProtection(tmp_path, demo=True)
    value = settings(['close_proxy', 'switch_node', 'close_tun'], switch_node=True, close_tun=False)
    reply = proxy.set_fallback(value)
    proxy.set_options({'auto_restore': True})
    proxy.set_client('flclash')
    reopened = ProxyProtection(tmp_path, demo=True)
    assert reopened.fallback == value
    assert reopened.auto_restore
    value['order'].reverse()
    reply['fallback']['enabled']['close_tun'] = True
    status = proxy.status()
    status['fallback']['order'].clear()
    assert reopened.fallback == proxy.fallback


def test_bad_settings_file_and_missing_options_keep_defaults(tmp_path):
    for content in ('{bad', '{}', '[]', '{"options": []}'):
        (tmp_path / 'routing-settings.json').write_text(content, encoding='utf-8')
        assert ProxyProtection(tmp_path, demo=True).fallback == default_fallback()


def test_failed_fallback_save_preserves_previous_value(tmp_path):
    proxy = ProxyProtection(tmp_path, demo=True)
    with patch.object(proxy, 'save', side_effect=OSError('private path')):
        with pytest.raises(ValueError, match='保存未完成'):
            proxy.set_fallback(settings(switch_node=True))
    assert proxy.fallback == default_fallback()


class ControlFixture(unittest.TestCase):
    setUp = test_proxy_guard.GuardFixture.setUp
    write_client_flags = test_proxy_guard.GuardFixture.write_client_flags
    exit_fixture = test_proxy_guard.GuardFixture.exit_fixture


class FallbackChainTests(ControlFixture):
    def setUp(self):
        super().setUp()
        self.exit_fixture()

    def tick(self):
        event = Mock()
        self.proxy.tick(threading.Event(), event, required_failures=1)
        return event

    def test_each_permutation_runs_in_order_and_summarizes_once(self):
        for order in itertools.permutations(default_fallback()['order']):
            with self.subTest(order=order):
                calls = []
                self.proxy.fallback = settings(order, switch_node=True)
                self.proxy.fail_open = Mock(side_effect=lambda only: calls.append('close_' + only[0]) or True)
                self.proxy._switch_node = Mock(side_effect=lambda cancel: calls.append('switch_node') or {'result': 'failed', 'detail': '同组没有可用节点'})
                event = self.tick()
                self.assertEqual(calls, list(order))
                self.assertEqual([row['step'] for row in self.proxy.last_fallback['steps']], list(order))
                self.assertEqual(self.proxy.state, 'direct')
                event.assert_called_once()

    def test_success_stops_following_actions_but_preserves_prior_closures(self):
        for order, closed in ((['switch_node', 'close_tun', 'close_proxy'], []),
                              (['close_proxy', 'switch_node', 'close_tun'], ['proxy'])):
            with self.subTest(order=order):
                self.proxy.fallback = settings(order, switch_node=True)
                self.proxy._switch_node = Mock(return_value={'result': 'done', 'detail': '已切换节点'})
                self.proxy.fail_open = Mock(return_value=True)
                self.tick()
                self.assertEqual([c.kwargs['only'][0] for c in self.proxy.fail_open.call_args_list], closed)
                self.assertEqual(self.proxy.last_fallback['steps'][-1]['result'], 'skipped')
                self.assertEqual(self.proxy.state, 'direct' if closed else 'healthy')

    def test_run_without_effect_is_held_instead_of_repeating_every_tick(self):
        # Exit stays down, nothing is left to close and no node works: the chain
        # must not re-measure the whole group and log again on every 15 s tick.
        self.proxy.fallback = settings(switch_node=True)
        self.proxy._switch_node = Mock(return_value={'result': 'failed', 'detail': '同组没有可用节点'})
        self.proxy.fail_open = Mock(return_value=False)
        first = self.tick()
        first.assert_called_once()
        state = (self.proxy.state, self.proxy.message)
        for _ in range(3):
            self.assertIsNone(self.proxy.tick(threading.Event(), Mock(), required_failures=1))
            self.assertEqual((self.proxy.state, self.proxy.message), state)
        self.assertEqual(self.proxy._switch_node.call_count, 1)
        # A check the user asked for always runs, but an identical outcome is not logged twice.
        manual = Mock()
        self.proxy.tick(threading.Event(), manual, required_failures=1, manual=True)
        self.assertEqual(self.proxy._switch_node.call_count, 2)
        manual.assert_not_called()
        # The hold expires after the retry interval.
        self.proxy.fallback_hold_until = time.monotonic() - 1
        self.tick()
        self.assertEqual(self.proxy._switch_node.call_count, 3)
        # A recovered exit ends the episode.
        self.proxy._probe_exit = Mock(return_value=True)
        self.tick()
        self.assertIsNone(self.proxy.fallback_hold_until)

    def test_disabled_steps_and_all_disabled_are_explicit(self):
        self.proxy.fallback = settings(switch_node=False, close_tun=False, close_proxy=False)
        self.proxy.fail_open = Mock()
        self.proxy._switch_node = Mock()
        event = self.tick()
        self.proxy.fail_open.assert_not_called()
        self.proxy._switch_node.assert_not_called()
        self.assertEqual((self.proxy.state, self.proxy.message), ('failed', '代理出口异常，故障处置均未开启'))
        self.assertTrue(all(row['result'] == 'skipped' for row in self.proxy.last_fallback['steps']))
        event.assert_called_once()

    def test_each_step_rechecks_cancellation_and_guard_option(self):
        for gate in ('cancel', 'allowed', 'switch_allowed'):
            with self.subTest(gate=gate):
                active = True
                cancel = threading.Event()
                def first(only):
                    nonlocal active
                    active = False
                    if gate == 'cancel':
                        cancel.set()
                    return True
                self.proxy.fail_open = Mock(side_effect=first)
                self.proxy.fallback = settings()
                self.proxy._run_fallback(cancel, lambda: active if gate == 'allowed' else True,
                    lambda: active if gate == 'switch_allowed' else True, lambda: False, True)
                self.proxy.fail_open.assert_called_once_with(only=['tun'])
                self.assertEqual(self.proxy.last_fallback['steps'][-1]['result'], 'skipped')

    def test_close_failure_does_not_block_the_other_field(self):
        self.proxy.fail_open = Mock(side_effect=[SwitchStateError('TUN 未确认'), True])
        self.tick()
        self.assertEqual([row['result'] for row in self.proxy.last_fallback['steps']], ['skipped', 'failed', 'done'])
        self.assertEqual(self.proxy.state, 'error')

    def test_selection_change_during_node_measurement_stops_later_closures(self):
        self.proxy.fallback = settings(switch_node=True)
        def changed(cancel):
            self.proxy.active_exit = Mock(side_effect=lambda: setattr(self.proxy, 'egress', 'Main → New') or 'New')
            return {'result': 'skipped', 'detail': '出口已切换，等待重新检测'}
        self.proxy._switch_node = Mock(side_effect=changed)
        self.proxy.fail_open = Mock()
        self.tick()
        self.proxy.fail_open.assert_not_called()
        self.assertEqual(self.proxy.state, 'uncertain')

    def test_real_field_closures_append_and_restore_in_reverse_order(self):
        self.proxy.fallback = settings(['close_proxy', 'close_tun', 'switch_node'])
        self.tick()
        self.assertEqual(self.gui, {'proxy': False, 'tun': False})
        self.assertEqual(self.proxy._snapshot()['fields'], ['proxy', 'tun'])
        self.switches.set.reset_mock()
        self.proxy.restore()
        self.assertEqual([c.args[0] for c in self.switches.set.call_args_list], ['tun', 'proxy'])


class IndependentSwitchTests(ControlFixture):
    def test_poll_reads_actual_state_without_bridge_and_detects_foreign_proxy(self):
        self.assertEqual(self.proxy.switch_status(), {'client': 'clash-verge', 'proxy': True,
            'tun': True, 'proxy_foreign': False, 'guarded': [], 'restoring': False})
        self.registry['ProxyServer'] = '127.0.0.1:9999'
        result = self.proxy.switch_status()
        self.assertIs(result['proxy'], False)
        self.assertTrue(result['proxy_foreign'])
        self.switches.read.assert_not_called()
        self.switches.set.assert_not_called()

    def test_poll_returns_null_for_unreadable_actual_states(self):
        self.proxy.system_proxy.side_effect = OSError('private')
        self.controller.request.side_effect = OSError('private')
        reply = self.proxy.switch_status()
        self.assertIsNone(reply['proxy'])
        self.assertIsNone(reply['tun'])
        self.assertNotIn('private', json.dumps(reply))
        self.switches.read.assert_not_called()

    def test_poll_exposes_only_completed_guard_fields_and_restore_flag(self):
        self.proxy.fail_open(only=['tun'])
        saved = self.proxy._snapshot() | {'restoring': True}
        self.proxy.recovery_path.write_text(json.dumps(saved), encoding='utf-8')
        self.switches.read.reset_mock()
        before = self.proxy.recovery_path.read_bytes()
        reply = self.proxy.switch_status()
        self.assertEqual(reply['guarded'], ['tun'])
        self.assertTrue(reply['restoring'])
        self.assertEqual(self.proxy.recovery_path.read_bytes(), before)
        self.switches.read.assert_not_called()

    def test_controller_down_still_reports_a_disabled_system_proxy(self):
        self.registry['ProxyEnable'] = 0
        self.controller.request.side_effect = OSError('offline')
        reply = self.proxy.switch_status()
        self.assertIs(reply['proxy'], False)
        self.assertIsNone(reply['tun'])

    def test_same_state_only_verifies_and_never_toggles(self):
        with patch.object(self.proxy, '_verify_switch', wraps=self.proxy._verify_switch) as verify:
            self.assertTrue(self.proxy.set_switch('proxy', True)['proxy'])
        verify.assert_called_once_with('proxy', True, {'port': 7897})
        self.switches.set.assert_not_called()

    def test_unknown_tun_state_never_verifies_as_disabled(self):
        self.gui['tun'] = False
        self.write_client_flags()
        self.config['tun'] = {}
        with self.assertRaises(SwitchStateError):
            self.proxy._verify_switch('tun', False, {'port': 7897}, wait=False)

    def test_manual_operation_takes_over_only_the_chosen_field(self):
        self.proxy.fail_open()
        saved = self.proxy._snapshot()
        saved['restored'] = ['tun', 'proxy']
        saved['restore_attempted'] = ['tun', 'proxy']
        self.proxy.recovery_path.write_text(json.dumps(saved), encoding='utf-8')
        result = self.proxy.set_switch('tun', True)
        self.assertEqual(result['guarded'], ['proxy'])
        for key in ('fields', 'completed', 'attempted', 'restored', 'restore_attempted'):
            self.assertNotIn('tun', self.proxy._snapshot()[key])
        self.proxy.flaps = 2
        self.proxy.set_switch('proxy', False)  # taking over an already-OFF field also retires it
        self.assertFalse(self.proxy.recovery_path.exists())
        self.assertEqual(self.proxy.flaps, 0)
        self.assertEqual(self.proxy.message, '已手动调整，继续检测出口')

    def test_incomplete_restore_rejects_manual_change_before_any_bridge_call(self):
        self.proxy.fail_open(only=['tun'])
        saved = self.proxy._snapshot() | {'restoring': True}
        self.proxy.recovery_path.write_text(json.dumps(saved), encoding='utf-8')
        self.switches.read.reset_mock()
        with self.assertRaisesRegex(ValueError, '代理恢复尚未完成，请先点击恢复代理'):
            self.proxy.set_switch('proxy', False)
        self.switches.read.assert_not_called()

    def test_failed_verification_keeps_recovery_ownership(self):
        self.proxy.fail_open(only=['tun'])
        with patch.object(self.proxy, '_verify_switch', side_effect=SwitchStateError('TUN 未确认')):
            with self.assertRaisesRegex(ValueError, 'TUN 未确认'):
                self.proxy.set_switch('tun', True)
        self.assertEqual(self.proxy._snapshot()['fields'], ['tun'])

    def test_tun_disabled_bridge_code_has_actionable_message(self):
        self.switches.set.side_effect = ClientControlError('代理客户端开关当前不可用', code='switch_disabled')
        with self.assertRaisesRegex(ValueError, 'TUN 开关不可用，请先在客户端中安装服务模式'):
            self.proxy.set_switch('tun', False)
        with self.assertRaisesRegex(ValueError, '^代理客户端开关当前不可用$'):
            self.proxy.set_switch('proxy', False)

    def test_invalid_field_type_and_stale_client_never_operate_bridge(self):
        for field, enabled, client in (('bad', True, None), ([], True, None),
                                      ('tun', 1, None), ('proxy', True, 'flclash')):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.proxy.set_switch(field, enabled, client=client)
        self.switches.read.assert_not_called()

    def test_fail_open_only_extends_original_from_current_gui(self):
        self.gui['tun'] = False
        self.config['tun']['enable'] = False
        self.write_client_flags()
        self.assertTrue(self.proxy.fail_open(only=['proxy']))
        self.assertFalse(self.proxy._snapshot()['original']['tun'])
        self.gui['tun'] = True
        self.config['tun']['enable'] = True
        self.write_client_flags()
        self.assertTrue(self.proxy.fail_open(only=['tun']))
        saved = self.proxy._snapshot()
        self.assertEqual(saved['fields'], ['proxy', 'tun'])
        self.assertTrue(saved['original']['tun'])
        self.assertTrue(self.proxy.fail_open(only=['tun']))
        self.assertEqual(saved, self.proxy._snapshot())

    def test_only_already_off_field_never_creates_recovery_or_visits_gui(self):
        self.gui['tun'] = False
        self.config['tun']['enable'] = False
        self.write_client_flags()
        self.assertFalse(self.proxy.fail_open(only=['tun']))
        self.assertFalse(self.proxy.recovery_path.exists())
        self.switches.read.assert_not_called()

    def test_partial_shutdown_never_resumes_when_direct_path_is_down(self):
        self.proxy.fail_open(only=['tun'])
        saved = self.proxy._snapshot()
        saved['completed'] = []
        self.proxy.recovery_path.write_text(json.dumps(saved), encoding='utf-8')
        self.proxy.direct_healthy = Mock(return_value=False)
        self.proxy.fail_open = Mock()
        self.proxy.tick(threading.Event(), Mock(), required_failures=1)
        self.proxy.fail_open.assert_not_called()
        self.assertEqual(self.proxy.message, '直连也未通过，保留代理设置')


def test_bridge_preserves_only_fixed_message_and_error_code(tmp_path):
    switch = ClientSwitches(tmp_path, 'clash-verge')
    reply = Mock(returncode=1, stdout=b'{"ok":false,"error":"switch_disabled"}')
    with patch.object(switch, '_executable', return_value=Path('fixture.exe')), patch('subprocess.run', return_value=reply):
        with pytest.raises(ClientControlError) as error:
            switch.set('tun', False, True)
    assert error.value.code == 'switch_disabled'


class NodeSwitchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.rows = {
            'Outer': {'type': 'Selector', 'all': ['Deep', 'Good'], 'now': 'Deep'},
            'Deep': {'type': 'Selector', 'all': ['Old', 'Fast', 'Good', 'DIRECT', 'REJECT',
                'REJECT-DROP', 'PASS', 'COMPATIBLE', 'AliasDirect', 'AliasReject'], 'now': 'Old'},
            'Old': {'type': 'SS'}, 'Fast': {'type': 'VLESS'}, 'Good': {'type': 'SS'},
            'DIRECT': {'type': 'Direct'}, 'REJECT': {'type': 'Reject'},
            'AliasDirect': {'type': 'Direct'}, 'AliasReject': {'type': 'RejectDrop'}}
        self.controller = Mock(root=self.root)
        self.controller.request.side_effect = self.request
        self.proxy = ProxyProtection(self.root, controller=self.controller)
        self.delays = {'Fast': 10, 'Good': 20, 'DIRECT': 1, 'AliasDirect': 1, 'AliasReject': 1}
        self.good = {'Good'}
        self.group_supported = True
        self.puts = []
        self.cancel = threading.Event()

    def request(self, method, path, payload=None):
        if path == '/configs':
            return {'mode': 'rule'}
        if path == '/rules':
            return {'rules': [{'type': 'Match', 'proxy': 'Outer'}]}
        if path == '/proxies':
            return {'proxies': deepcopy(self.rows)}
        if method == 'PUT':
            group = urllib.parse.unquote(path.removeprefix('/proxies/'))
            self.puts.append((group, payload['name']))
            self.rows[group]['now'] = payload['name']
            return {}
        if path.startswith('/group/'):
            if not self.group_supported:
                raise ControllerError(404)
            return self.delays
        if '/delay?' in path:
            name = urllib.parse.unquote(path.split('/')[2])
            if name in self.good:
                return {'delay': self.delays.get(name, 50)}
            raise ControllerError(504)
        raise AssertionError('Unexpected fake controller request')

    def test_deepest_selector_excludes_nonproxy_types_and_checks_two_choices(self):
        outcome = self.proxy._switch_node(self.cancel)
        self.assertEqual(outcome['result'], 'done')
        self.assertEqual(self.puts, [('Deep', 'Fast'), ('Deep', 'Good')])
        self.assertEqual(self.proxy.exit_chain, ['Outer', 'Deep', 'Good'])
        self.assertEqual(self.proxy.exit_delay, 20)
        self.assertEqual(self.proxy.exit_state, 'healthy')
        self.assertIn('从 Old 切换至 Good（20 ms）', outcome['detail'])

    def test_failed_two_probes_roll_back_and_read_back_original_selection(self):
        self.good = set()
        outcome = self.proxy._switch_node(self.cancel)
        self.assertEqual(outcome, {'result': 'failed', 'detail': '切换后出口仍未通过'})
        self.assertEqual(self.puts, [('Deep', 'Fast'), ('Deep', 'Good'), ('Deep', 'Old')])
        self.assertEqual(self.proxy.exit_chain, ['Outer', 'Deep', 'Old'])
        self.assertIsNone(self.proxy.exit_delay)

    def test_unsupported_group_endpoint_uses_bounded_individual_delays(self):
        self.group_supported = False
        self.assertEqual(self.proxy._switch_node(self.cancel)['result'], 'done')
        self.assertEqual(self.puts, [('Deep', 'Good')])
        paths = [c.args[1] for c in self.controller.request.call_args_list]
        self.assertFalse(any('/proxies/DIRECT/delay' in path or '/proxies/Alias' in path for path in paths))

    def test_at_most_twelve_individual_measurements(self):
        names = [f'New{i}' for i in range(20)]
        self.rows['Deep']['all'] = ['Old'] + names
        self.rows.update({name: {'type': 'SS'} for name in names})
        self.group_supported = False
        self.proxy._switch_node(self.cancel)
        measured = [c for c in self.controller.request.call_args_list if '/delay?' in c.args[1] and c.args[1].startswith('/proxies/')]
        self.assertEqual(len(measured), 12)
        self.assertEqual(self.puts, [])

    def test_invalid_delays_are_never_candidates(self):
        for invalid in (True, 0, -1, 3501, '20', None, float('nan'), float('inf')):
            with self.subTest(value=invalid):
                self.delays = {'Fast': invalid, 'Good': invalid}
                self.assertEqual(self.proxy._switch_node(self.cancel)['detail'], '同组没有可用节点')
        self.assertEqual(self.puts, [])

    def test_automatic_chain_or_no_alternatives_is_skipped(self):
        self.rows['Outer']['type'] = 'URLTest'
        self.rows['Deep']['type'] = 'URLTest'
        self.assertEqual(self.proxy._switch_node(self.cancel)['detail'], '当前出口由客户端自动选择')
        self.rows['Deep']['type'] = 'Selector'
        self.rows['Deep']['all'] = ['Old', 'DIRECT', 'AliasReject']
        self.assertEqual(self.proxy._switch_node(self.cancel)['detail'], '同组没有其他节点')

    def test_three_rounds_pause_until_window_expires_and_no_probe_runs_when_paused(self):
        self.good = set()
        for _ in range(3):
            self.proxy._switch_node(self.cancel)
        self.assertTrue(self.proxy.status()['node_switch_paused'])
        self.controller.request.reset_mock()
        self.assertEqual(self.proxy._switch_node(self.cancel)['detail'], '节点切换过于频繁，已暂停')
        self.controller.request.assert_not_called()
        self.proxy.node_switches = [time.monotonic() - 1801] * 3
        self.assertFalse(self.proxy.status()['node_switch_paused'])

    def test_manual_reselection_and_fallback_changes_clear_rate_protection(self):
        self.proxy.node_switches = [time.monotonic()] * 3
        self.proxy.select_node({'client': self.proxy.client, 'group': 'Deep', 'node': 'Old'})
        self.assertFalse(self.proxy.node_switch_paused)
        self.proxy.node_switches = [time.monotonic()] * 3
        self.proxy.set_fallback(default_fallback())
        self.assertFalse(self.proxy.node_switch_paused)

    def test_cancellation_after_put_rolls_back_original(self):
        original = self.controller.request.side_effect
        def request(method, path, payload=None):
            reply = original(method, path, payload)
            if method == 'PUT':
                self.cancel.set()
            return reply
        self.controller.request.side_effect = request
        self.proxy._switch_node(self.cancel)
        self.assertEqual(self.puts, [('Deep', 'Fast'), ('Deep', 'Old')])

    def test_demo_and_precancelled_runs_do_not_call_controller(self):
        self.proxy.demo = True
        self.assertEqual(self.proxy._switch_node(self.cancel)['result'], 'skipped')
        self.proxy.demo = False
        self.cancel.set()
        self.assertEqual(self.proxy._switch_node(self.cancel)['result'], 'skipped')
        self.controller.request.assert_not_called()

    def test_authorization_error_never_fans_out_to_individual_measurements(self):
        original = self.controller.request.side_effect
        def request(method, path, payload=None):
            if path.startswith('/group/'):
                raise ControllerError(401)
            return original(method, path, payload)
        self.controller.request.side_effect = request
        self.assertEqual(self.proxy._switch_node(self.cancel)['result'], 'failed')
        self.assertFalse(any('/proxies/' in c.args[1] for c in self.controller.request.call_args_list))
        self.assertEqual(self.puts, [])

    def test_measurement_selection_race_does_not_overwrite_manual_choice(self):
        original = self.controller.request.side_effect
        def request(method, path, payload=None):
            reply = original(method, path, payload)
            if path.startswith('/group/'):
                self.rows['Deep']['now'] = 'Good'
            return reply
        self.controller.request.side_effect = request
        self.assertEqual(self.proxy._switch_node(self.cancel)['detail'], '出口已切换，等待重新检测')
        self.assertEqual(self.puts, [])

    def test_failed_rollback_readback_is_reported_as_uncertain(self):
        self.good = set()
        original = self.controller.request.side_effect
        def request(method, path, payload=None):
            if method == 'PUT' and payload['name'] == 'Old':
                return {}
            return original(method, path, payload)
        self.controller.request.side_effect = request
        self.assertEqual(self.proxy._switch_node(self.cancel)['detail'], '原节点恢复未确认，请在客户端核对')
        self.assertEqual(self.proxy.exit_state, 'uncertain')

    def test_group_names_are_encoded_in_measurement_and_selection_paths(self):
        group = '深层 / 香港%'
        self.rows[group] = self.rows.pop('Deep')
        self.rows['Outer']['now'] = group
        self.rows['Outer']['all'] = [group, 'Good']
        self.assertEqual(self.proxy._switch_node(self.cancel)['result'], 'done')
        encoded = urllib.parse.quote(group, safe='')
        self.controller.request.assert_any_call('PUT', '/proxies/' + encoded, {'name': 'Good'})
        self.assertTrue(any(c.args[1].startswith('/group/' + encoded + '/delay?') for c in self.controller.request.call_args_list))

    def test_subgroups_are_valid_alternatives_and_health_probes_their_leaf(self):
        self.rows['Sub'] = {'type': 'URLTest', 'all': ['Good'], 'now': 'Good'}
        self.rows['Deep']['all'] = ['Old', 'Sub']
        self.delays = {'Sub': 10, 'Good': 15}
        self.assertEqual(self.proxy._switch_node(self.cancel)['result'], 'done')
        self.assertEqual(self.proxy.exit_chain, ['Outer', 'Deep', 'Sub', 'Good'])


class RecoveryNodeRetryTests(ControlFixture):
    def setUp(self):
        super().setUp()
        self.exit_fixture()
        self.proxy.fail_open()
        self.saved = self.proxy._snapshot()
        self.proxy.fallback = settings(switch_node=True)
        self.proxy._switch_node = Mock(return_value={'result': 'failed', 'detail': '同组没有可用节点'})

    def recover(self, automatic=False, switch_allowed=True):
        return self.proxy._await_recovery(threading.Event(), lambda: True, self.saved,
                                         lambda: automatic, lambda: switch_allowed)

    def test_failed_retries_are_limited_to_once_every_five_minutes(self):
        self.recover()
        self.recover()
        self.proxy._switch_node.assert_called_once()
        self.proxy.node_retry_at -= 301
        self.recover()
        self.assertEqual(self.proxy._switch_node.call_count, 2)

    def test_recovery_respects_guard_switch_direct_path_and_rate_pause(self):
        self.recover(switch_allowed=False)
        self.proxy.direct_healthy.return_value = False
        self.recover()
        self.proxy.direct_healthy.return_value = True
        self.proxy.node_switches = [time.monotonic()] * 3
        self.recover()
        self.proxy._switch_node.assert_not_called()

    def test_success_waits_for_manual_restore_and_reports_summary(self):
        def succeed(cancel):
            self.exit_dead = False
            self.proxy._probe_exit()
            return {'result': 'done', 'detail': '已切换节点'}
        self.proxy._switch_node.side_effect = succeed
        notice = self.recover()
        self.assertEqual(self.proxy.message, '已切换节点，出口恢复，等待手动恢复代理')
        self.assertIn('已切换节点', notice[1])
        self.assertTrue(self.proxy.recovery_path.exists())

    def test_retry_success_uses_existing_stability_rounds_before_auto_restore(self):
        def succeed(cancel):
            self.exit_dead = False
            self.proxy._probe_exit()
            return {'result': 'done', 'detail': '已切换节点'}
        self.proxy._switch_node.side_effect = succeed
        self.recover(automatic=True)
        self.assertEqual(self.proxy.recoveries, 1)
        for _ in range(2):
            self.recover(automatic=True)
        self.assertEqual(self.gui, {'proxy': False, 'tun': False})
        self.recover(automatic=True)
        self.assertEqual(self.gui, {'proxy': True, 'tun': True})

    def test_restore_option_toggle_does_not_bypass_retry_cooldown(self):
        self.recover()
        self.proxy.set_options({'auto_restore': True})
        self.proxy.set_options({'auto_restore': False})
        self.recover()
        self.proxy._switch_node.assert_called_once()


def verge_fixture(path, *, kind='remote', name='"示例\\n订阅"', extra=True):
    text = f'''current: selected-private-id
items:
  - uid: script-private-id
    type: script
    name: script
  - uid: selected-private-id
    type: {kind}
    name: {name}
    updated: 1700000000
    url: "https://subscription.invalid/private-token
    file: private-profile-path
    option:
      name: do-not-use
    selected:
      - name: do-not-use-either
        now: x
'''
    if extra:
        text += '    extra:\n      upload: 10\n      download: 20\n      total: 100\n      expire: 0\n'
    path.write_text(text, encoding='utf-8')


def assert_private_free(reply, caplog):
    text = json.dumps(reply) + caplog.text
    for private in ('https://subscription.invalid', 'private-token', 'private-profile-path',
                    'selected-private-id', 'age-secret-fixture', 'url', 'uid'):
        assert private not in text


def test_verge_allowlist_ignores_even_malformed_private_values(tmp_path, caplog):
    verge_fixture(tmp_path / 'profiles.yaml')
    reply = SubscriptionReader().read('clash-verge', tmp_path)
    assert reply == {'client': 'clash-verge', 'available': True, 'name': '示例订阅', 'kind': 'remote',
                     'updated_at': '2023-11-14T22:13:20+00:00',
                     'usage': {'used': 30, 'total': 100, 'expire': ''}, 'message': ''}
    assert_private_free(reply, caplog)


@pytest.mark.parametrize('indent', ['', '  '])
def test_verge_both_sequence_indents_and_single_quotes(tmp_path, indent):
    text = "current: 'id'\nitems:\n" + indent + "- uid: id\n" + indent + "  type: remote\n" + indent + "  name: 'A ''quoted'' name'\n"
    (tmp_path / 'profiles.yaml').write_text(text, encoding='utf-8')
    reply = SubscriptionReader().read('clash-verge', tmp_path)
    assert reply['name'] == "A 'quoted' name"
    assert reply['usage'] is None


def test_verge_cache_uses_mtime_and_does_not_return_mutable_internal_state(tmp_path):
    path = tmp_path / 'profiles.yaml'
    verge_fixture(path)
    reader = SubscriptionReader()
    first = reader.read('clash-verge', tmp_path)
    first['usage']['used'] = 999
    with patch('njust_wifi.subscription._verge', side_effect=AssertionError('cached data must not be reparsed')):
        assert reader.read('clash-verge', tmp_path)['usage']['used'] == 30
    verge_fixture(path, name='Changed')
    os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 1000000))
    assert reader.read('clash-verge', tmp_path)['name'] == 'Changed'


@pytest.mark.parametrize('content', ['', 'broken', 'current: id\nitems: [bad]',
    'current: id\nitems:\n- uid: id\n  type: remote\n  name: "broken',
    'current: id\nitems:\n- uid: id\n  type: remote\n  name: a\n  name: b',
    'current: id\nitems:\n- uid: id\n  type: remote\n  name: a\n  updated: bad'])
def test_bad_verge_returns_unavailable_without_private_error_text(tmp_path, caplog, content):
    (tmp_path / 'profiles.yaml').write_text(content, encoding='utf-8')
    reply = SubscriptionReader().read('clash-verge', tmp_path)
    assert not reply['available']
    assert_private_free(reply, caplog)


def test_local_and_unknown_profiles_hide_all_summary_fields(tmp_path):
    for kind in ('local', 'script'):
        verge_fixture(tmp_path / 'profiles.yaml', kind=kind)
        reply = SubscriptionReader().read('clash-verge', tmp_path)
        assert not reply['available']
        assert reply['name'] == reply['kind'] == ''
        assert reply['updated_at'] is reply['usage'] is None
        if kind == 'local':
            assert reply['message'] == '当前使用本地配置'


def test_subscription_name_is_bounded_and_control_characters_are_removed(tmp_path):
    name = json.dumps('x' * 140 + '\x00\n\u200b', ensure_ascii=False)
    verge_fixture(tmp_path / 'profiles.yaml', name=name, extra=False)
    assert SubscriptionReader().read('clash-verge', tmp_path)['name'] == 'x' * 128


def test_missing_file_cache_notices_new_subscription(tmp_path):
    reader = SubscriptionReader()
    assert not reader.read('clash-verge', tmp_path)['available']
    verge_fixture(tmp_path / 'profiles.yaml')
    assert reader.read('clash-verge', tmp_path)['available']


def make_flclash(root, *, kind='url', info=None):
    (root / 'shared_preferences.json').write_text(json.dumps({'flutter.config': json.dumps({'currentProfileId': 7})}), encoding='utf-8')
    with closing(sqlite3.connect(root / 'database.sqlite')) as connection, connection:
        connection.execute('CREATE TABLE profiles (id INTEGER, type TEXT, label TEXT, last_update_date INTEGER, subscription_info TEXT, url TEXT, age_secret_key TEXT)')
        connection.execute('INSERT INTO profiles VALUES (?, ?, ?, ?, ?, ?, ?)',
            (7, kind, 'Fl 订阅', 1700000000123, json.dumps(info if info is not None else
             {'upload': 11, 'download': 22, 'total': 0, 'expire': 1700001000}),
             'https://subscription.invalid/private-token', 'age-secret-fixture'))


def test_flclash_queries_only_public_columns_with_read_only_connection(tmp_path, caplog):
    make_flclash(tmp_path)
    queries, modes = [], []
    original = sqlite3.connect
    def connect(database, **kwargs):
        modes.append((database.endswith('?mode=ro'), kwargs.get('uri')))
        connection = original(database, **kwargs)
        connection.set_trace_callback(queries.append)
        return connection
    with patch('njust_wifi.subscription.sqlite3.connect', side_effect=connect):
        reply = SubscriptionReader().read('flclash', tmp_path)
    assert modes == [(True, True)]
    assert len(queries) == 1
    assert 'url' not in queries[0] and 'age_secret_key' not in queries[0]
    assert reply['available'] and reply['name'] == 'Fl 订阅'
    assert reply['updated_at'] == '2023-11-14T22:13:20+00:00'
    assert reply['usage'] == {'used': 33, 'total': 0, 'expire': '2023-11-14T22:30:00+00:00'}
    assert_private_free(reply, caplog)


@pytest.mark.parametrize('kind', ['clash-verge', 'flclash'])
def test_missing_subscription_files_are_not_created_and_never_raise(tmp_path, caplog, kind):
    reply = SubscriptionReader().read(kind, tmp_path)
    assert not reply['available']
    assert list(tmp_path.iterdir()) == []
    assert_private_free(reply, caplog)


@pytest.mark.parametrize('broken', ['prefs', 'id', 'database', 'table', 'info'])
def test_bad_flclash_data_fails_closed(tmp_path, broken):
    make_flclash(tmp_path)
    if broken == 'prefs':
        (tmp_path / 'shared_preferences.json').write_text('{bad')
    elif broken == 'id':
        (tmp_path / 'shared_preferences.json').write_text(json.dumps({'flutter.config': '{"currentProfileId":true}'}))
    elif broken == 'database':
        (tmp_path / 'database.sqlite').write_bytes(b'broken')
    else:
        with closing(sqlite3.connect(tmp_path / 'database.sqlite')) as connection, connection:
            connection.execute('DROP TABLE profiles' if broken == 'table' else "UPDATE profiles SET subscription_info='bad'")
    assert not SubscriptionReader().read('flclash', tmp_path)['available']


def test_flclash_cache_invalidates_for_preferences_database_and_wal(tmp_path):
    make_flclash(tmp_path)
    reader = SubscriptionReader()
    assert reader.read('flclash', tmp_path)['available']
    for name in ('shared_preferences.json', 'database.sqlite', 'database.sqlite-wal'):
        path = tmp_path / name
        if not path.exists():
            path.touch()
        os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 1000000))
        with patch('njust_wifi.subscription._flclash', return_value=('remote', 'Changed', None, None, 1000)) as load:
            assert reader.read('flclash', tmp_path)['name'] == 'Changed'
            load.assert_called_once()


def test_flclash_profile_types_map_to_subscription_kinds(tmp_path):
    # Real FlClash databases store 'url' for subscriptions and 'file' for imported files.
    make_flclash(tmp_path, kind='url')
    reply = SubscriptionReader().read('flclash', tmp_path)
    assert reply['available'] and reply['kind'] == 'remote'
    with closing(sqlite3.connect(tmp_path / 'database.sqlite')) as connection, connection:
        connection.execute("UPDATE profiles SET type = 'file'")
    reply = SubscriptionReader().read('flclash', tmp_path)
    assert not reply['available'] and reply['message'] == '当前使用本地配置'


def test_flclash_long_term_unlimited_and_missing_usage(tmp_path):
    make_flclash(tmp_path, info={'upload': 1, 'download': 2, 'total': 0, 'expire': 0})
    assert SubscriptionReader().read('flclash', tmp_path)['usage'] == {'used': 3, 'total': 0, 'expire': ''}
    with closing(sqlite3.connect(tmp_path / 'database.sqlite')) as connection, connection:
        connection.execute('UPDATE profiles SET subscription_info = NULL')
    assert SubscriptionReader().read('flclash', tmp_path)['usage'] is None


class ControlsHttpTests(unittest.TestCase):
    setUp = test_app.ApiTests.setUp
    tearDown = test_app.ApiTests.tearDown
    call = test_app.ApiTests.call

    def test_demo_gets_and_post_switch_roundtrip_never_touch_real_clients(self):
        proxy = self.service.proxy
        with (patch.object(proxy, 'system_proxy', side_effect=AssertionError('no registry')),
             patch.object(proxy.controller, 'request', side_effect=AssertionError('no core')),
             patch.object(proxy.controller, 'runtime', side_effect=AssertionError('no real files')),
             patch.object(proxy.switches, 'read', side_effect=AssertionError('no bridge'))):
            code, reply = self.call('/api/proxy/switches')
            self.assertEqual((code, reply['proxy'], reply['tun'], reply['guarded']), (200, True, False, []))
            self.assertTrue(self.call('/api/proxy/subscription')[1]['available'])
            code, reply = self.call('/api/proxy/switch', {'client': proxy.client, 'field': 'tun', 'enabled': True})
            self.assertEqual((code, reply['tun']), (200, True))
            self.assertTrue(self.call('/api/proxy/switches')[1]['tun'])
            status = self.call('/api/proxy')[1]
            self.assertEqual(status['exit_delay'], 48)
            self.assertIsNone(status['last_fallback'])

    def test_fallback_post_returns_exact_contract_and_audit_label(self):
        value = settings(['close_tun', 'switch_node', 'close_proxy'], switch_node=True)
        self.assertEqual(self.call('/api/proxy/fallback', value), (200, {'fallback': value}))
        self.assertEqual(self.call('/api/proxy')[1]['fallback'], value)
        self.assertIn('设置故障处置顺序：已完成', [row['message'] for row in self.service.logs()])

    def test_new_routes_require_session_host_and_origin(self):
        for path, body in (('/api/proxy/switches', None), ('/api/proxy/subscription', None),
            ('/api/proxy/switch', {'client': 'clash-verge', 'field': 'tun', 'enabled': True}),
            ('/api/proxy/fallback', default_fallback())):
            for headers in ({'X-NJUST-Token': ''}, {'Host': 'other.test'}, {'Origin': 'https://other.test'}):
                with self.subTest(path=path, headers=headers):
                    self.assertEqual(self.call(path, body, headers)[0], 403)

    def test_invalid_posts_return_400_fixed_messages_and_failed_audits(self):
        self.assertEqual(self.call('/api/proxy/fallback', {})[0], 400)
        for raw in ({'client': 'flclash', 'field': 'tun', 'enabled': True},
                    {'client': 'clash-verge', 'field': [], 'enabled': True},
                    {'client': 'clash-verge', 'field': 'tun', 'enabled': 1}, {}):
            with self.subTest(raw=raw):
                self.assertEqual(self.call('/api/proxy/switch', raw)[0], 400)
        logs = [row['message'] for row in self.service.logs()]
        self.assertIn('设置故障处置顺序：未完成', logs)
        self.assertIn('切换 TUN · 开启：未完成', logs)

    def test_switch_labels_and_failed_exit_warning_follow_protection_setting(self):
        self.service.proxy.exit_state = 'failed'
        self.service.store.settings['proxy_fail_open'] = True
        code, reply = self.call('/api/proxy/switch', {'client': 'clash-verge', 'field': 'proxy', 'enabled': True})
        self.assertEqual((code, reply['warning']), (200, '出口仍异常，故障保护复核后会再次关闭'))
        self.service.store.settings['proxy_fail_open'] = False
        self.assertNotIn('warning', self.call('/api/proxy/switch', {'client': 'clash-verge', 'field': 'tun', 'enabled': True})[1])
        self.call('/api/proxy/switch', {'client': 'clash-verge', 'field': 'proxy', 'enabled': False})
        logs = [row['message'] for row in self.service.logs()]
        for label in ('切换系统代理 · 开启', '切换系统代理 · 关闭', '切换 TUN · 开启'):
            self.assertIn(label + '：已完成', logs)

    def test_asset_allowlist_does_not_allow_arbitrary_files(self):
        from njust_wifi.server import ASSETS
        self.assertEqual(ASSETS['/proxy-controls.js'], ('proxy-controls.js', 'text/javascript; charset=utf-8'))
        self.assertEqual(self.call('/../njust_wifi/proxy.py')[0], 404)

    def test_tun_bridge_disabled_reaches_http_as_400_with_failed_audit(self):
        self.service.proxy.demo = False
        self.service.proxy.controller = Mock(root=self.store.root)
        self.service.proxy.controller.request.return_value = {'mixed-port': 7897}
        self.service.proxy.switches = Mock()
        self.service.proxy.switches.read.return_value = {'proxy': False, 'tun': False}
        self.service.proxy.switches.set.side_effect = ClientControlError('disabled', code='switch_disabled')
        code, reply = self.call('/api/proxy/switch', {'client': 'clash-verge', 'field': 'tun', 'enabled': True})
        self.assertEqual((code, reply), (400, {'error': 'TUN 开关不可用，请先在客户端中安装服务模式'}))
        self.assertIn('切换 TUN · 开启：未完成', [row['message'] for row in self.service.logs()])
