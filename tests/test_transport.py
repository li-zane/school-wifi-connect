"""Proxy/TUN branches use synthetic sockets; never change the host network."""
import io
import json
from pathlib import Path
import socket
import ssl
import struct
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.request

from njust_wifi.network import Network, direct_opener
from njust_wifi.service import Service
from njust_wifi.storage import Store
from njust_wifi.transport import (
    AdaptiveOpener, BoundHTTPConnection, BoundHTTPSConnection, DirectPathUnavailable,
    NoBoundRedirect, WifiBinding, WifiResolver, bound_socket, fake_ip, parse_dns,
)

BINDING = WifiBinding(4, '192.0.2.10', ('192.0.2.53', '192.0.2.54'))
HOST = 'portal.example.test'
URL = 'http://' + HOST + '/probe'


def response(body=b'', status=200):
    result = io.BytesIO(body)
    result.status = status
    return result


def name(host):
    return b''.join(bytes([len(p)]) + p.encode() for p in host.split('.')) + b'\0'


def answer(owner, kind, data):
    return owner + struct.pack('!HHIH', kind, 1, 60, len(data)) + data


def dns_packet(ident=123, host=HOST, rows=None, flags=0x8180):
    rows = rows if rows is not None else [answer(b'\xc0\x0c', 1, socket.inet_aton('192.0.2.20'))]
    return struct.pack('!HHHHHH', ident, flags, 1, len(rows), 0, 0) + name(host) + struct.pack('!HH', 1, 1) + b''.join(rows)


class DnsTests(unittest.TestCase):
    def test_real_answers_cname_compression_and_unrelated_filter(self):
        rows = [answer(b'\xc0\x0c', 5, name('edge.example.test')),
                answer(name('edge.example.test'), 1, socket.inet_aton('192.0.2.21')),
                answer(name('unrelated.test'), 1, socket.inet_aton('192.0.2.99')),
                answer(b'\xc0\x0c', 1, socket.inet_aton('198.18.0.1'))]
        self.assertEqual(parse_dns(dns_packet(rows=rows), 123, HOST), ['192.0.2.21'])

    def test_rejects_id_question_truncation_opcode_and_errors(self):
        packets = [dns_packet(ident=124), dns_packet(host='other.test'),
                   dns_packet(flags=0x8380), dns_packet(flags=0x8980),
                   dns_packet(flags=0x8183), dns_packet(flags=0x0180),
                   dns_packet()[:-1], b'\x00']
        for packet in packets:
            with self.subTest(packet=packet), self.assertRaises((ValueError, IndexError, struct.error)):
                parse_dns(packet, 123, HOST)

    def test_pointer_cycle_is_bounded_and_fake_range_is_exact(self):
        packet = struct.pack('!HHHHHH', 123, 0x8180, 1, 0, 0, 0) + b'\xc0\x0c'
        with self.assertRaises(ValueError):
            parse_dns(packet, 123, HOST)
        for ip in ('198.18.0.0', '198.19.255.255'):
            self.assertTrue(fake_ip(ip))
        for ip in ('198.17.255.255', '198.20.0.0', '::1', 'invalid'):
            self.assertFalse(fake_ip(ip))

    def test_resolver_uses_bound_dns_and_caches_only_real_answers(self):
        stream = Mock()
        stream.__enter__ = Mock(return_value=stream)
        stream.__exit__ = Mock(return_value=False)
        stream.recv.return_value = dns_packet()
        resolver = WifiResolver(BINDING)
        with patch('njust_wifi.transport.secrets.randbits', return_value=123), patch('njust_wifi.transport.bound_socket', return_value=stream) as create:
            self.assertEqual(resolver.resolve(HOST, time.monotonic() + 5), ['192.0.2.20'])
            self.assertEqual(resolver.resolve(HOST, time.monotonic() + 5), ['192.0.2.20'])
        self.assertEqual(create.call_count, 1)
        stream.connect.assert_called_once_with(('192.0.2.53', 53))
        self.assertIn(name(HOST), stream.send.call_args.args[0])

    def test_dns_failure_falls_to_second_server_with_deadline(self):
        stream = Mock()
        stream.__enter__ = Mock(return_value=stream)
        stream.__exit__ = Mock(return_value=False)
        stream.recv.side_effect = [socket.timeout(), dns_packet()]
        with patch('njust_wifi.transport.secrets.randbits', return_value=123), patch('njust_wifi.transport.bound_socket', return_value=stream):
            self.assertEqual(WifiResolver(BINDING).resolve(HOST, time.monotonic() + 5), ['192.0.2.20'])
        self.assertEqual([call.args[0] for call in stream.connect.call_args_list], [('192.0.2.53', 53), ('192.0.2.54', 53)])
        with patch('njust_wifi.transport.bound_socket') as create, self.assertRaises(OSError):
            WifiResolver(BINDING).resolve(HOST, time.monotonic() - 1)
        create.assert_not_called()

    def test_literal_fake_ip_and_cancellation_never_connect(self):
        resolver = WifiResolver(BINDING, threading.Event())
        self.assertEqual(resolver.resolve('192.0.2.20', time.monotonic() + 5), ['192.0.2.20'])
        with self.assertRaises(OSError):
            resolver.resolve('198.18.0.1', time.monotonic() + 5)
        resolver.cancel.set()
        with patch('njust_wifi.transport.bound_socket') as create, self.assertRaises(InterruptedError):
            resolver.connect(HOST, 80, 5)
        create.assert_not_called()


class SocketTests(unittest.TestCase):
    def test_binding_uses_network_order_and_closes_on_failure(self):
        stream = Mock()
        with patch('njust_wifi.transport.socket.socket', return_value=stream):
            self.assertIs(bound_socket(BINDING, socket.SOCK_STREAM, 3), stream)
            stream.setsockopt.assert_called_once_with(socket.IPPROTO_IP, 31, struct.pack('!I', 4))
            stream.bind.assert_called_once_with(('192.0.2.10', 0))
            stream.bind.side_effect = OSError('fixture')
            with self.assertRaises(OSError):
                bound_socket(BINDING, socket.SOCK_STREAM, 3)
        stream.close.assert_called_once()

    def test_http_retains_hostname_host_header_and_bound_destination(self):
        resolver = Mock()
        stream = resolver.connect.return_value
        connection = BoundHTTPConnection(HOST, resolver=resolver, timeout=3)
        connection.request('GET', '/probe')
        resolver.connect.assert_called_once_with(HOST, 80, 3)
        self.assertIn(('Host: ' + HOST).encode(), stream.sendall.call_args.args[0])
        connection.close()

    def test_https_preserves_sni_and_verification_and_closes_tls_failure(self):
        resolver = Mock()
        connection = BoundHTTPSConnection(HOST, resolver=resolver, timeout=3)
        self.assertTrue(connection._context.check_hostname)
        self.assertEqual(connection._context.verify_mode, ssl.CERT_REQUIRED)
        context = Mock()
        connection._context = context
        connection.connect()
        context.wrap_socket.assert_called_once_with(resolver.connect.return_value, server_hostname=HOST)
        context.wrap_socket.side_effect = ssl.SSLCertVerificationError('fixture')
        with self.assertRaises(ssl.SSLCertVerificationError):
            connection.connect()
        resolver.connect.return_value.close.assert_called_once()


class AdaptiveTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = self.enterContext(patch('njust_wifi.transport.adapter_snapshot', return_value=((BINDING,), False)))
        self.dns = self.enterContext(patch('njust_wifi.transport.socket.getaddrinfo', return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('192.0.2.20', 0))]))
        self.normal = Mock(handlers=[])
        self.opener = AdaptiveOpener(self.normal, threading.Event())

    def test_normal_branch_is_lazy_and_skips_proxy(self):
        self.snapshot.assert_not_called()
        self.assertIs(self.opener.open(URL), self.normal.open.return_value)
        self.assertEqual(self.opener.mode, 'normal')
        self.assertFalse(self.opener.compatibility_active)
        with patch.dict('os.environ', {'HTTP_PROXY': 'http://fixture.invalid:9999'}):
            self.assertFalse(any(isinstance(h, urllib.request.ProxyHandler) and h.proxies for h in direct_opener().handlers))

    def test_tun_and_fake_ip_choose_wifi_before_any_request(self):
        for tun, address in ((True, '192.0.2.20'), (False, '198.19.0.1')):
            self.snapshot.return_value = ((BINDING,), tun)
            self.dns.return_value = [(2, 1, 6, '', (address, 0))]
            opener = AdaptiveOpener(self.normal)
            opener.bound = Mock()
            self.assertIs(opener.open(URL), opener.bound.open.return_value)
            self.assertEqual(opener.mode, 'wifi-direct')
            self.assertTrue(opener.compatibility_active)
        self.normal.open.assert_not_called()

    def test_get_failure_uses_wifi_but_post_timeout_is_never_replayed(self):
        self.normal.open.side_effect = urllib.error.URLError('fixture-private-error')
        self.opener.bound = Mock()
        self.assertIs(self.opener.open(URL), self.opener.bound.open.return_value)
        self.assertEqual(self.opener.mode, 'wifi-direct')
        post = AdaptiveOpener(self.normal)
        with patch.object(post, '_bound_open') as bound, self.assertRaises(urllib.error.URLError):
            post.open(urllib.request.Request(URL, data=b'fixture-secret'))
        bound.assert_not_called()
        self.assertFalse(post.force_wifi)

    def test_bound_post_is_submitted_once_and_raw_errors_are_hidden(self):
        self.snapshot.return_value = ((BINDING,), True)
        self.opener.bound = Mock()
        self.opener.bound.open.side_effect = OSError('fixture-secret at 192.0.2.10')
        with self.assertRaises(DirectPathUnavailable) as caught:
            self.opener.open(urllib.request.Request(URL, data=b'fixture-secret'))
        self.assertNotIn('fixture-secret', str(caught.exception))
        self.assertNotIn('192.0.2.10', str(caught.exception))
        self.assertEqual(self.opener.bound.open.call_count, 1)
        self.normal.open.assert_not_called()
        self.assertEqual(self.opener.mode, 'unavailable')

    def test_tun_without_physical_binding_stops_without_proxy_fallback(self):
        self.snapshot.return_value = ((), True)
        with self.assertRaises(DirectPathUnavailable):
            self.opener.open(URL)
        self.normal.open.assert_not_called()

    def test_http_error_is_not_misclassified_or_retried(self):
        for bound in (False, True):
            self.opener.invalidate()
            self.snapshot.return_value = ((BINDING,), bound)
            error = urllib.error.HTTPError(URL, 401, 'fixture', {}, None)
            self.normal.open.side_effect = error
            self.opener.bound = Mock()
            self.opener.bound.open.side_effect = error
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.opener.open(URL)
            self.assertIs(caught.exception, error)
            self.assertEqual(self.opener.mode, 'wifi-direct' if bound else 'normal')
            error.close()

    def test_cookie_jar_shared_and_redirects_disabled(self):
        normal = direct_opener()
        opener = AdaptiveOpener(normal)
        opener.snapshot = ((BINDING,), True)
        with patch('urllib.request.OpenerDirector.open', return_value=response()):
            opener._bound_open(URL, 3)
        jar = next(h.cookiejar for h in normal.handlers if isinstance(h, urllib.request.HTTPCookieProcessor))
        self.assertIs(next(h.cookiejar for h in opener.bound.handlers if isinstance(h, urllib.request.HTTPCookieProcessor)), jar)
        self.assertFalse(any(isinstance(h, urllib.request.ProxyHandler) and h.proxies for h in opener.bound.handlers))
        self.assertIsNone(NoBoundRedirect().redirect_request(None, None, 302, '', {}, URL))

    def test_cancel_before_or_during_failure_does_not_retry(self):
        self.opener.cancel.set()
        with self.assertRaises(InterruptedError):
            self.opener.open(URL)
        self.normal.open.assert_not_called()
        self.opener.cancel.clear()
        def failure(*args, **kwargs):
            self.opener.cancel.set()
            raise OSError('fixture')
        self.normal.open.side_effect = failure
        with patch.object(self.opener, '_bound_open') as bound, self.assertRaises(InterruptedError):
            self.opener.open(URL)
        bound.assert_not_called()

    def test_invalidation_discards_dns_bindings_and_cookies_context(self):
        self.opener.snapshot = ((BINDING,), True)
        self.opener.bound = Mock()
        self.opener.force_wifi = self.opener.compatibility_active = True
        self.opener.mode = 'wifi-direct'
        self.opener.checked_hosts.add(HOST)
        self.opener.invalidate()
        self.assertIsNone(self.opener.snapshot)
        self.assertIsNone(self.opener.bound)
        self.assertEqual(self.opener.checked_hosts, set())
        self.assertEqual(self.opener.mode, 'normal')
        self.assertFalse(self.opener.compatibility_active)


class RecoveryBranchTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch('njust_wifi.network.restore_wifi_radio', return_value=False))

    def network(self, current='NJUST', uncertain=True):
        network = Network(threading.Event())
        network.current_ssid = Mock(return_value=current)
        network.connect = Mock()
        network.login = Mock()
        network.online = Mock(return_value=False)
        network.campus_session = Mock(return_value=None)
        network.opener.compatibility_active = True
        network.path_uncertain = uncertain
        return network

    def settings(self):
        return {'ssid': 'NJUST', 'domain': 'default', 'fallback_networks': [{'ssid': 'backup', 'password': ''}]}

    def test_authenticated_campus_preserved_when_external_probes_fail(self):
        network = self.network(uncertain=False)
        network.campus_session.return_value = True
        event = Mock()
        with self.assertRaises(DirectPathUnavailable):
            network.recover(self.settings(), 'fixture', 'fixture-secret', event)
        network.connect.assert_not_called()
        network.login.assert_not_called()
        event.assert_not_called()

    def test_uncertain_campus_or_backup_does_not_switch_or_login(self):
        for current in ('NJUST', 'backup'):
            network = self.network(current)
            with self.assertRaises(DirectPathUnavailable):
                network.recover(self.settings(), 'fixture', 'fixture-secret', Mock())
            network.connect.assert_not_called()
            network.login.assert_not_called()

    def test_explicit_unauthenticated_allows_login_without_reassociation(self):
        network = self.network()
        network.campus_session.return_value = False
        network.online.side_effect = [False, False, True]
        network.recover(self.settings(), 'fixture', 'fixture-secret', Mock())
        network.connect.assert_not_called()
        network.login.assert_called_once_with('fixture', 'fixture-secret', 'default')

    def test_uncertainty_during_auth_does_not_switch_to_backup(self):
        network = self.network()
        network.campus_session.return_value = False
        network.login.side_effect = DirectPathUnavailable()
        with self.assertRaises(DirectPathUnavailable):
            network.recover(self.settings(), 'fixture', 'fixture-secret', Mock())
        network.connect.assert_not_called()
        self.assertEqual(network.login.call_count, 1)

    def test_new_association_or_successful_auth_preserves_confirmed_session(self):
        for newly_associated in (True, False):
            network = self.network(current='' if newly_associated else 'NJUST')
            network.campus_session.side_effect = [True] if newly_associated else [False, False, True]
            with self.assertRaises(DirectPathUnavailable):
                network.recover(self.settings(), 'fixture', 'fixture-secret', Mock())
            self.assertEqual(network.login.call_count, 0 if newly_associated else 1)
            self.assertEqual([c.args[0] for c in network.connect.call_args_list], ['NJUST'] if newly_associated else [])

    def test_new_backup_with_uncertain_transport_is_preserved(self):
        network = self.network(current='')
        with self.assertRaises(DirectPathUnavailable):
            network.recover({'ssid': '', 'fallback_networks': [{'ssid': 'first', 'password': ''}, {'ssid': 'second', 'password': ''}]}, '', '', Mock())
        network.connect.assert_called_once_with('first', password='')
        network.login.assert_not_called()

    def test_read_only_session_evidence_and_bounded_unknowns(self):
        network = Network(threading.Event())
        network.opener = Mock()
        for payload, expected in (({'reply_code': 0, 'results': {'username': 'fixture'}}, True),
                                  ({'reply_code': 1}, None), ({'reply_code': False}, None),
                                  ({'reply_code': 0, 'results': {}}, None), ([], None)):
            network.opener.open.return_value = response(json.dumps(payload).encode())
            self.assertIs(network.campus_session(), expected)
        network.opener.open.return_value = response(b'x' * 65537)
        self.assertIsNone(network.campus_session())
        request = network.opener.open.call_args.args[0]
        self.assertTrue(request.endswith('/api/selfservice/v1/userinfo'))
        for code, expected in ((401, False), (403, False), (500, None)):
            network.opener.open.side_effect = urllib.error.HTTPError(URL, code, '', {}, io.BytesIO())
            self.assertIs(network.campus_session(), expected)

    def test_cancel_propagates_in_session_login_and_probe(self):
        network = Network(threading.Event())
        network.opener = Mock()
        network.opener.open.side_effect = InterruptedError()
        for action in (network.campus_session, lambda: network.login('u', 'p', 'default'), network.online):
            with self.assertRaises(InterruptedError):
                action()

    def test_http_mismatch_retries_bound_path_but_not_indefinitely(self):
        network = Network(threading.Event())
        with patch('njust_wifi.transport.adapter_snapshot', return_value=((BINDING,), False)), patch('njust_wifi.transport.socket.getaddrinfo', return_value=[]):
            network.opener.normal.open = Mock(side_effect=lambda *a, **k: response(b'login', 200))
            network.opener.bound = Mock()
            network.opener.bound.open.side_effect = lambda *a, **k: response(b'', 204)
            self.assertTrue(network.online())
            self.assertEqual(network.opener.mode, 'wifi-direct')
            self.assertEqual(network.opener.normal.open.call_count, 2)
            self.assertEqual(network.opener.bound.open.call_count, 2)
            self.assertFalse(network.path_uncertain)

    def test_service_records_only_allowlisted_path_with_error_backoff(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp))
            store.update({'fallback_networks': [{'ssid': 'fixture', 'password': ''}]})
            network = Mock()
            network.opener.mode = 'unavailable'
            network.recover.side_effect = DirectPathUnavailable()
            service = Service(store, network_factory=lambda _: network)
            service.guards.check = Mock()
            try:
                service.start()
                deadline = time.monotonic() + 3
                while not service.checked_at or service.busy:
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(.01)
                self.assertEqual(service.status()['connection_path'], 'unavailable')
                self.assertEqual(service.failures, 1)
                self.assertIsNotNone(service.next_check_at)
                self.assertNotIn('192.0.2', json.dumps(service.diagnostics()))
            finally:
                service.close()


if __name__ == '__main__':
    unittest.main()
