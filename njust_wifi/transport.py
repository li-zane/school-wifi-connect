"""Socket-local WiFi transport. No changes to adapters, routes, DNS or proxies."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import http.client
import ipaddress
import os
import secrets
import socket
import struct
import time
import urllib.error
import urllib.parse
import urllib.request


class DirectPathUnavailable(RuntimeError):
    def __init__(self, message='直连通路暂不可确认，已保留当前 WiFi，请检查代理或 DNS'):
        super().__init__(message)


@dataclass(frozen=True)
class WifiBinding:
    index: int
    address: str
    dns: tuple[str, ...]


class SocketAddress(ctypes.Structure):
    _fields_ = [('pointer', ctypes.c_void_p), ('length', ctypes.c_int)]


class AddressNode(ctypes.Structure):
    pass


AddressNode._fields_ = [('alignment', ctypes.c_ulonglong), ('next', ctypes.POINTER(AddressNode)),
                       ('address', SocketAddress)]


class UnicastNode(ctypes.Structure):
    pass


UnicastNode._fields_ = [('alignment', ctypes.c_ulonglong), ('next', ctypes.POINTER(UnicastNode)),
                       ('address', SocketAddress), ('prefix_origin', wintypes.ULONG),
                       ('suffix_origin', wintypes.ULONG), ('dad_state', wintypes.ULONG)]


class AdapterNode(ctypes.Structure):
    pass


AdapterNode._fields_ = [('length', wintypes.ULONG), ('index', wintypes.ULONG),
                       ('next', ctypes.POINTER(AdapterNode)), ('name', ctypes.c_char_p),
                       ('unicast', ctypes.POINTER(UnicastNode)), ('anycast', ctypes.c_void_p),
                       ('multicast', ctypes.c_void_p), ('dns', ctypes.POINTER(AddressNode)),
                       ('dns_suffix', wintypes.LPWSTR), ('description', wintypes.LPWSTR),
                       ('friendly_name', wintypes.LPWSTR), ('physical_address', ctypes.c_ubyte * 8),
                       ('physical_length', wintypes.ULONG), ('flags', wintypes.ULONG),
                       ('mtu', wintypes.ULONG), ('kind', wintypes.ULONG), ('status', wintypes.ULONG)]


def _ipv4(address):
    if not address.pointer or address.length < 16:
        return None
    data = ctypes.string_at(address.pointer, 16)
    if struct.unpack_from('=H', data)[0] != socket.AF_INET:
        return None
    ip = ipaddress.ip_address(data[4:8])
    return str(ip) if not (ip.is_loopback or ip.is_unspecified or ip.is_link_local or ip.is_multicast) else None


def adapter_snapshot() -> tuple[tuple[WifiBinding, ...], bool]:
    if os.name != 'nt':
        return (), False
    try:
        function = ctypes.WinDLL('iphlpapi').GetAdaptersAddresses
        function.argtypes = [wintypes.ULONG, wintypes.ULONG, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(wintypes.ULONG)]
        function.restype = wintypes.ULONG
        size = wintypes.ULONG(16384)
        for _ in range(2):
            if not 0 < size.value <= 1048576:
                return (), False
            buffer = ctypes.create_string_buffer(size.value)
            result = function(socket.AF_INET, 6, None, buffer, ctypes.byref(size))
            if result != 111:
                break
        if result:
            return (), False
        pointer = ctypes.cast(buffer, ctypes.POINTER(AdapterNode))
        bindings, tun = [], False
        for _ in range(128):
            if not pointer:
                break
            item = pointer.contents
            if item.status == 1:
                label = ((item.description or '') + ' ' + (item.friendly_name or '')).casefold()
                tun |= any(word in label for word in ('mihomo', 'meta tunnel', 'clash', 'sing-box', 'wintun'))
                if item.kind == 71:  # IF_TYPE_IEEE80211; exclude virtual Ethernet/VPN interfaces.
                    addresses, dns = [], []
                    node = item.unicast
                    for _ in range(64):
                        if not node:
                            break
                        address = _ipv4(node.contents.address)
                        if address and node.contents.dad_state == 4:
                            addresses.append(address)
                        node = node.contents.next
                    node = item.dns
                    for _ in range(8):
                        if not node:
                            break
                        address = _ipv4(node.contents.address)
                        if address:
                            dns.append(address)
                        node = node.contents.next
                    if addresses and dns:
                        bindings.append(WifiBinding(item.index, addresses[0], tuple(dict.fromkeys(dns[:2]))))
            pointer = item.next
        return tuple(bindings[:8]), tun
    except (OSError, AttributeError, ValueError):
        return (), False


def fake_ip(address: str) -> bool:
    try:
        return ipaddress.ip_address(address) in ipaddress.ip_network('198.18.0.0/15')
    except ValueError:
        return False


def bound_socket(binding, kind, timeout):
    result = socket.socket(socket.AF_INET, kind)
    try:
        result.settimeout(max(.05, timeout))
        result.setsockopt(socket.IPPROTO_IP, 31, struct.pack('!I', binding.index))
        result.bind((binding.address, 0))
        return result
    except BaseException:
        result.close()
        raise


def _dns_name(packet, offset, seen=None):
    seen = set() if seen is None else seen
    if offset in seen or len(seen) > 16:
        raise ValueError('Invalid DNS pointer')
    seen.add(offset)
    labels, end = [], None
    for _ in range(128):
        size = packet[offset]
        offset += 1
        if size & 0xc0 == 0xc0:
            target = ((size & 63) << 8) | packet[offset]
            name, _ = _dns_name(packet, target, seen)
            labels.append(name)
            end = offset + 1
            break
        if size == 0:
            end = offset
            break
        if size > 63 or offset + size > len(packet):
            raise ValueError('Invalid DNS label')
        labels.append(packet[offset:offset + size].decode('ascii').lower())
        offset += size
    if end is None:
        raise ValueError('Invalid DNS name')
    return '.'.join(labels), end


def parse_dns(packet, ident, host):
    got, flags, questions, answers, _, _ = struct.unpack_from('!HHHHHH', packet)
    if got != ident or not flags & 0x8000 or flags & 0x7a0f or questions != 1 or answers > 128:
        raise ValueError('Invalid DNS response')
    question, offset = _dns_name(packet, 12)
    if question != host or struct.unpack_from('!HH', packet, offset) != (1, 1):
        raise ValueError('Unmatched DNS question')
    offset += 4
    aliases, rows = {}, []
    for _ in range(answers):
        name, offset = _dns_name(packet, offset)
        kind, klass, ttl, length = struct.unpack_from('!HHIH', packet, offset)
        offset += 10
        if offset + length > len(packet):
            raise ValueError('Truncated DNS answer')
        if klass == 1 and kind == 5:
            aliases[name] = _dns_name(packet, offset)[0]
        if klass == 1 and kind == 1 and length == 4:
            rows.append((name, socket.inet_ntoa(packet[offset:offset + 4])))
        offset += length
    allowed = {host}
    for _ in range(8):
        allowed |= {target for name, target in aliases.items() if name in allowed}
    return list(dict.fromkeys(ip for name, ip in rows if name in allowed and not fake_ip(ip)))


class WifiResolver:
    def __init__(self, binding, cancel=None):
        self.binding, self.cancel, self.cache = binding, cancel, {}

    def active(self):
        if self.cancel and self.cancel.is_set():
            raise InterruptedError()

    def resolve(self, host, deadline):
        self.active()
        try:
            address = str(ipaddress.IPv4Address(host))
        except ipaddress.AddressValueError:
            pass
        else:
            if fake_ip(address):
                raise OSError('Real destination unavailable')
            return [address]
        host = host.rstrip('.').encode('idna').decode('ascii').lower()
        if host in self.cache:
            return self.cache[host]
        parts = host.split('.')
        if len(host) > 253 or any(not 0 < len(part) <= 63 for part in parts):
            raise OSError('DNS unavailable')
        for server in self.binding.dns:
            self.active()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            ident = secrets.randbits(16)
            query = struct.pack('!HHHHHH', ident, 0x0100, 1, 0, 0, 0) + b''.join(bytes([len(p)]) + p.encode('ascii') for p in parts) + b'\0' + struct.pack('!HH', 1, 1)
            try:
                with bound_socket(self.binding, socket.SOCK_DGRAM, min(1.5, remaining)) as stream:
                    stream.connect((server, 53))
                    stream.send(query)
                    addresses = parse_dns(stream.recv(4096), ident, host)
                if addresses:
                    self.cache[host] = addresses
                    return addresses
            except (OSError, ValueError, IndexError, struct.error):
                continue
        raise OSError('Campus DNS unavailable')

    def connect(self, host, port, timeout):
        deadline = time.monotonic() + timeout
        for ip in self.resolve(host, deadline):
            self.active()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            stream = bound_socket(self.binding, socket.SOCK_STREAM, remaining)
            try:
                stream.connect((ip, port))
                stream.settimeout(max(.05, deadline - time.monotonic()))
                return stream
            except OSError:
                stream.close()
        raise OSError('Bound connection unavailable')


class BoundHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host, *, resolver, **kwargs):
        super().__init__(host, **kwargs)
        self.resolver = resolver

    def connect(self):
        self.sock = self.resolver.connect(self.host, self.port, self.timeout)


class BoundHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, *, resolver, **kwargs):
        super().__init__(host, **kwargs)
        self.resolver = resolver

    def connect(self):
        stream = self.resolver.connect(self.host, self.port, self.timeout)
        try:
            self.sock = self._context.wrap_socket(stream, server_hostname=self.host)
        except BaseException:
            stream.close()
            raise


class BoundHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, resolver):
        super().__init__()
        self.resolver = resolver

    def http_open(self, request):
        return self.do_open(lambda host, **kwargs: BoundHTTPConnection(host, resolver=self.resolver, **kwargs), request)


class BoundHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, resolver):
        super().__init__()
        self.resolver = resolver

    def https_open(self, request):
        return self.do_open(lambda host, **kwargs: BoundHTTPSConnection(host, resolver=self.resolver, **kwargs), request)


class NoBoundRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class AdaptiveOpener:
    def __init__(self, normal, cancel=None):
        self.normal, self.cancel = normal, cancel
        self.mode = 'normal'
        self.compatibility_active = False
        self.invalidate()

    def invalidate(self):
        self.snapshot = None
        self.bound = None
        self.force_wifi = False
        self.compatibility_active = False
        self.mode = 'normal'
        self.checked_hosts = set()

    def active(self):
        if self.cancel and self.cancel.is_set():
            raise InterruptedError()

    def try_wifi_path(self):
        if self.snapshot is None:
            self.snapshot = adapter_snapshot()
        if not self.snapshot[0]:
            return False
        self.force_wifi = self.compatibility_active = True
        return True

    def _bound_open(self, request, timeout):
        bindings = self.snapshot[0]
        if not bindings:
            self.mode = 'unavailable'
            raise DirectPathUnavailable()
        if self.bound is None:
            resolver = WifiResolver(bindings[0], self.cancel)
            cookie = next((handler for handler in self.normal.handlers if isinstance(handler, urllib.request.HTTPCookieProcessor)), None)
            handlers = [urllib.request.ProxyHandler({}), NoBoundRedirect(), BoundHTTPHandler(resolver), BoundHTTPSHandler(resolver)]
            if cookie:
                handlers.append(urllib.request.HTTPCookieProcessor(cookie.cookiejar))
            self.bound = urllib.request.build_opener(*handlers)
        self.mode = 'wifi-direct'
        try:
            return self.bound.open(request, timeout=timeout)
        except urllib.error.HTTPError:
            raise  # An HTTP response is evidence that the route reached a server.
        except (OSError, urllib.error.URLError):
            self.active()
            self.mode = 'unavailable'
            raise DirectPathUnavailable() from None

    def open(self, request, timeout=5):
        self.active()
        request = urllib.request.Request(request) if isinstance(request, str) else request
        host = urllib.parse.urlsplit(request.full_url).hostname
        if self.snapshot is None:
            self.snapshot = adapter_snapshot()
        if self.snapshot[1]:
            self.force_wifi = self.compatibility_active = True
        if not self.force_wifi and host not in self.checked_hosts:
            self.checked_hosts.add(host)
            try:
                if any(fake_ip(row[4][0]) for row in socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)):
                    self.force_wifi = self.compatibility_active = True
            except OSError:
                pass
        if self.force_wifi:
            return self._bound_open(request, timeout)
        try:
            self.mode = 'normal'
            return self.normal.open(request, timeout=timeout)
        except urllib.error.HTTPError:
            raise
        except (OSError, urllib.error.URLError):
            self.active()
            # Never resubmit a possibly accepted login POST after a timeout.
            if request.get_method() in ('GET', 'HEAD') and self.try_wifi_path():
                return self._bound_open(request, timeout)
            raise
