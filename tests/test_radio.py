"""Radio recovery tests never toggle a real WiFi radio."""
import ctypes
from ctypes import wintypes
import threading
import unittest
from unittest.mock import Mock, patch

from njust_wifi.network import Network
from njust_wifi.wlan import Guid, Interface, PhyRadioState, RadioController, restore_wifi_radio


def guid(number=1):
    value = Guid()
    value.data1 = number
    return value


def row(software=2, hardware=1, index=0, adapter=1):
    return (guid(adapter), index, software, hardware)


class RadioTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch('njust_wifi.wlan.os.name', 'nt'))
        factory = self.enterContext(patch('njust_wifi.wlan.RadioController'))
        self.radio = factory.return_value.__enter__.return_value
        self.cancel = threading.Event()
        self.event = Mock()

    def restore(self):
        return restore_wifi_radio(self.cancel, self.event)

    def test_healthy_radio_never_changes_or_logs(self):
        self.radio.states.return_value = [row(1), row(adapter=2)]
        self.assertFalse(self.restore())
        self.radio.enable.assert_not_called()
        self.event.assert_not_called()

    def test_off_radio_is_enabled_verified_and_logged(self):
        self.radio.states.side_effect = [[row()], [row(1)]]
        self.assertTrue(self.restore())
        self.assertEqual(self.radio.enable.call_count, 1)
        self.assertEqual(self.radio.enable.call_args.args[1], 0)
        self.assertEqual([c.args for c in self.event.call_args_list], [
            ('recovering', 'WiFi 已关闭，正在尝试开启'),
            ('recovering', 'WiFi 已开启，正在恢复连接')])

    def test_all_off_phys_of_one_adapter_only(self):
        self.radio.states.side_effect = [[row(index=0), row(index=1), row(adapter=2)], [row(1, index=0), row(1, index=1)]]
        self.assertTrue(self.restore())
        self.assertEqual([c.args[1] for c in self.radio.enable.call_args_list], [0, 1])
        self.assertTrue(all(bytes(c.args[0]) == bytes(guid(1)) for c in self.radio.enable.call_args_list))

    def test_hardware_off_unknown_or_missing_never_sets_radio(self):
        for states in ([row(hardware=2)], [row(0)], []):
            self.radio.states.return_value = states
            with self.assertRaises(RuntimeError):
                self.restore()
        self.radio.enable.assert_not_called()
        self.event.assert_not_called()

    def test_cancel_before_query_or_write_propagates(self):
        self.cancel.set()
        with self.assertRaises(InterruptedError):
            self.restore()
        self.radio.states.assert_not_called()
        self.cancel.clear()
        self.radio.states.return_value = [row()]
        self.event.side_effect = lambda *args: self.cancel.set()
        with self.assertRaises(InterruptedError):
            self.restore()
        self.radio.enable.assert_not_called()

    def test_pause_during_verification_does_not_retry_or_turn_off(self):
        self.radio.states.return_value = [row()]
        self.cancel.wait = Mock(return_value=True)
        with self.assertRaises(InterruptedError):
            self.restore()
        self.radio.enable.assert_called_once()
        self.assertEqual(self.event.call_count, 1)

    def test_verification_timeout_is_bounded_and_does_not_report_success(self):
        self.radio.states.return_value = [row()]
        with patch('njust_wifi.wlan.time.monotonic', side_effect=[0, 5]), self.assertRaisesRegex(RuntimeError, '稍后重试'):
            self.restore()
        self.radio.enable.assert_called_once()
        self.assertEqual(self.event.call_count, 1)

    def test_api_error_is_fixed_and_contains_no_adapter_or_private_path(self):
        self.radio.states.return_value = [row()]
        self.radio.enable.side_effect = OSError('fixture-private adapter path 192.0.2.1')
        with self.assertRaises(RuntimeError) as caught:
            self.restore()
        self.assertNotIn('fixture-private', str(caught.exception))
        self.assertNotIn('192.0.2', str(caught.exception))


class NativeRadioTests(unittest.TestCase):
    def setUp(self):
        self.api = Mock()
        self.enterContext(patch('njust_wifi.wlan.ctypes.WinDLL', return_value=self.api))
        def opened(version, reserved, negotiated, handle):
            handle._obj.value = 123
            negotiated._obj.value = 2
            return 0
        self.api.WlanOpenHandle.side_effect = opened
        self.api.WlanEnumInterfaces.return_value = 0
        self.api.WlanQueryInterface.return_value = 0
        self.api.WlanSetInterface.return_value = 0
        self.buffers = []
        self.setup_interfaces(1)
        self.setup_query([PhyRadioState(3, 2, 1)])

    def setup_interfaces(self, count):
        buffer = ctypes.create_string_buffer(8 + ctypes.sizeof(Interface))
        self.buffers.append(buffer)
        ctypes.cast(buffer, ctypes.POINTER(wintypes.DWORD))[0] = count
        Interface.from_buffer(buffer, 8).guid = guid()
        def enumerated(handle, reserved, result):
            result._obj.value = ctypes.addressof(buffer)
            return 0
        self.api.WlanEnumInterfaces.side_effect = enumerated

    def setup_query(self, states, count=None, size=None, status=0):
        buffer = ctypes.create_string_buffer(4 + len(states) * ctypes.sizeof(PhyRadioState))
        self.buffers.append(buffer)
        ctypes.cast(buffer, ctypes.POINTER(wintypes.DWORD))[0] = len(states) if count is None else count
        for i, state in enumerate(states):
            ctypes.memmove(ctypes.addressof(buffer) + 4 + i * ctypes.sizeof(state), ctypes.byref(state), ctypes.sizeof(state))
        def query(handle, interface, opcode, reserved, length, data, kind):
            self.assertEqual(opcode, 4)
            length._obj.value = ctypes.sizeof(buffer) if size is None else size
            data._obj.value = ctypes.addressof(buffer)
            return status
        self.api.WlanQueryInterface.side_effect = query

    def test_query_copies_guid_and_frees_native_memory_and_handle(self):
        with RadioController() as controller:
            rows = controller.states()
        self.assertEqual([(r[1], r[2], r[3]) for r in rows], [(3, 2, 1)])
        self.assertEqual(bytes(rows[0][0]), bytes(guid()))
        self.assertEqual(self.api.WlanFreeMemory.call_count, 2)
        self.api.WlanCloseHandle.assert_called_once()
        self.api.WlanSetInterface.assert_not_called()

    def test_set_passes_one_phy_with_only_software_state_on(self):
        with RadioController() as controller:
            controller.enable(guid(), 3)
        args = self.api.WlanSetInterface.call_args.args
        self.assertEqual(args[2:4], (4, ctypes.sizeof(PhyRadioState)))
        state = ctypes.cast(args[4], ctypes.POINTER(PhyRadioState)).contents
        self.assertEqual((state.index, state.software, state.hardware), (3, 1, 0))

    def test_native_set_access_denied_is_fixed_and_handle_closed(self):
        self.api.WlanSetInterface.return_value = 5
        with self.assertRaisesRegex(RuntimeError, '系统权限'):
            with RadioController() as controller:
                controller.enable(guid(), 3)
        self.api.WlanCloseHandle.assert_called_once()

    def test_malformed_query_or_enum_is_bounded_and_freed(self):
        for size, count, status in ((3, 1, 0), (16, 65, 0), (4, 1, 0), (16, 1, 5)):
            self.setup_query([PhyRadioState(3, 2, 1)], count=count, size=size, status=status)
            self.api.WlanFreeMemory.reset_mock()
            with self.assertRaises(RuntimeError):
                with RadioController() as controller:
                    controller.states()
            self.assertEqual(self.api.WlanFreeMemory.call_count, 2)
        self.setup_interfaces(65)
        with self.assertRaises(RuntimeError):
            with RadioController() as controller:
                controller.states()

    def test_open_failure_does_not_query_or_set(self):
        self.api.WlanOpenHandle.side_effect = None
        self.api.WlanOpenHandle.return_value = 1062
        with self.assertRaisesRegex(RuntimeError, '无线网络服务'):
            with RadioController():
                self.fail('Failed native session entered')
        self.api.WlanEnumInterfaces.assert_not_called()
        self.api.WlanSetInterface.assert_not_called()
        self.api.WlanCloseHandle.assert_not_called()


class RadioRecoveryTests(unittest.TestCase):
    def network(self):
        network = Network(threading.Event())
        network.current_ssid = Mock(return_value='NJUST')
        network.online = Mock(return_value=True)
        network.connect = Mock()
        network.login = Mock()
        return network

    def test_radio_recovery_precedes_probe_and_resets_bound_context(self):
        network = self.network()
        event = Mock()
        network.opener.snapshot = ((), True)
        network.opener.force_wifi = True
        network.current_ssid.side_effect = ['', 'NJUST']
        with patch('njust_wifi.network.restore_wifi_radio', return_value=True) as restore:
            network.online.side_effect = lambda: (restore.assert_called_once_with(network.cancel, event), True)[1]
            network.recover({'ssid': 'NJUST', 'fallback_networks': []}, 'u', 'p', event)
        self.assertIsNone(network.opener.snapshot)
        self.assertFalse(network.opener.force_wifi)
        network.connect.assert_not_called()
        network.login.assert_not_called()

    def test_no_configured_network_never_enables_radio(self):
        network = self.network()
        with patch('njust_wifi.network.restore_wifi_radio') as restore:
            network.recover({'ssid': '', 'fallback_networks': []}, '', '', Mock())
        restore.assert_not_called()

    def test_associated_wifi_skips_radio_api_entirely(self):
        network = self.network()
        with patch('njust_wifi.network.restore_wifi_radio') as restore:
            network.recover({'ssid': 'NJUST', 'fallback_networks': []}, 'u', 'p', Mock())
        restore.assert_not_called()
        network.connect.assert_not_called()

    def test_failed_radio_restore_ends_cycle_before_probes_or_connection(self):
        network = self.network()
        network.current_ssid.return_value = ''
        with patch('njust_wifi.network.restore_wifi_radio', side_effect=RuntimeError('WiFi 开关尚未开启，将稍后重试')):
            with self.assertRaises(RuntimeError):
                network.recover({'ssid': 'NJUST', 'fallback_networks': []}, 'u', 'p', Mock())
        network.current_ssid.assert_called_once()
        network.online.assert_not_called()
        network.connect.assert_not_called()
        network.login.assert_not_called()


if __name__ == '__main__':
    unittest.main()
