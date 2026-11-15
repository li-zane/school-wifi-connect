"""Local HTTP UI with host/origin checks, per-process token and strict assets."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from . import __version__
import secrets
import socket

ASSETS = {"/": ("index.html", "text/html; charset=utf-8"),
          "/index.html": ("index.html", "text/html; charset=utf-8"),
          "/styles.css": ("styles.css", "text/css; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/onboarding.js": ("onboarding.js", "text/javascript; charset=utf-8"),
          "/autosave.js": ("autosave.js", "text/javascript; charset=utf-8"),
          "/window.js": ("window.js", "text/javascript; charset=utf-8"),
          "/proxy.js": ("proxy.js", "text/javascript; charset=utf-8"),
          "/updates.js": ("updates.js", "text/javascript; charset=utf-8"),
          "/onboarding.css": ("onboarding.css", "text/css; charset=utf-8"),
          "/logo.webp": ("logo.webp", "image/webp")}
ASSETS.update({"/proxy-guide.js":("proxy-guide.js","text/javascript; charset=utf-8"),
               "/proxy-guide.css":("proxy-guide.css","text/css; charset=utf-8")})
for _image in ('verge-settings','verge-controller','flclash-settings','flclash-controller',
               'njust-controller-verge','njust-controller-flclash'):
    ASSETS['/guide/'+_image+'.webp']=('guide/'+_image+'.webp','image/webp')
MAX_BODY = 16384


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False
    request_queue_size = 8

    def __init__(self, service, assets: Path, port: int = 18089):
        self.service, self.assets = service, assets
        self.token = secrets.token_urlsafe(32)
        self.slots = __import__("threading").BoundedSemaphore(8)
        super().__init__(("127.0.0.1", port), Handler)
        self.origin = f"http://127.0.0.1:{self.server_port}"

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        # Request bodies and tokens must never reach stderr or tracebacks.
        pass


class Handler(BaseHTTPRequestHandler):
    server_version = "NJUSTWiFi"
    sys_version = ""

    def setup(self):
        self.request.settimeout(5)
        super().setup()

    def log_message(self, *args):
        pass

    def send(self, status: int, content, mime="application/json; charset=utf-8"):
        data = json.dumps(content, ensure_ascii=False).encode() if not isinstance(content, bytes) else content
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; font-src 'self'; frame-ancestors 'none'; form-action 'none'; base-uri 'none'")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            pass

    def trusted(self, *, token: bool = True) -> bool:
        host = self.server.origin.removeprefix("http://")
        if self.headers.get("Host") != host:
            self.send(403, {"error": "请求主机不匹配"})
            return False
        if self.headers.get("Origin", self.server.origin) != self.server.origin:
            self.send(403, {"error": "请求来源不匹配"})
            return False
        if self.headers.get("Sec-Fetch-Site", "none") not in ("none", "same-origin"):
            self.send(403, {"error": "仅接受本地应用请求"})
            return False
        if token and not secrets.compare_digest(self.headers.get("X-NJUST-Token", ""), self.server.token):
            self.send(403, {"error": "会话已过期，请刷新页面"})
            return False
        return True

    def do_GET(self):
        if not self.trusted(token=self.path.startswith("/api/") and self.path not in ("/api/session", "/api/health")):
            return
        service = self.server.service
        if self.path == "/api/health":
            self.send(200, {"app": "NJUSTWiFi", "version": __version__})
        elif self.path == "/api/session":
            self.send(200, {"token": self.server.token})
        elif self.path == "/api/status":
            self.send(200, service.status())
        elif self.path == "/api/balance":
            self.send(200, service.balance())
        elif self.path == "/api/config":
            self.send(200, service.config_view())
        elif self.path == "/api/update":
            self.send(200, service.updater.status())
        elif self.path == "/api/proxy":
            self.send(200, service.proxy.status() | {'enabled':service.store.settings['proxy_fail_open']})
        elif self.path == "/api/proxy/controller":
            self.send(200, service.proxy.controller_view())
        elif self.path == "/api/proxy/groups":
            try:
                self.send(200, {'groups':service.proxy.groups()})
                service.audit(service.operation(self.path, {}))
            except ValueError as exc:
                service.audit(service.operation(self.path, {}), False)
                self.send(400, {'error':str(exc)})
        elif self.path == "/api/credentials":
            with service.lock:
                self.send(200, service.store.credentials())
        elif self.path == "/api/logs":
            self.send(200, {"entries": service.logs()})
        elif self.path == "/api/diagnostics":
            self.send(200, service.diagnostics())
        elif self.path == "/api/probes":
            self.send(200, {"probes": service.probe_items()})
        elif self.path == "/api/probes/candidates":
            try:
                self.send(200, {"candidates": service.candidates()})
            except (ValueError, OSError):
                self.send(400, {"error": "程序列表读取未完成"})
        elif self.path in ASSETS:
            filename, mime = ASSETS[self.path]
            try:
                self.send(200, (self.server.assets / filename).read_bytes(), mime)
            except OSError:
                self.send(503, {"error": "前端资源尚未就绪"})
        else:
            self.send(404, {"error": "页面不存在"})

    def do_POST(self):
        operation = None
        if not self.trusted():
            return
        if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
            self.send(415, {"error": "请使用 JSON 请求"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY or self.headers.get("Transfer-Encoding"):
                self.send(413, {"error": "请求体过大或为空"})
                return
            raw = json.loads(self.rfile.read(length))
            if not isinstance(raw, dict):
                raise ValueError("请求格式不正确")
            service = self.server.service
            operation = service.operation(self.path, raw)
            if self.path == "/api/config":
                service.configure(raw)
            elif self.path in ('/api/update/check', '/api/update/download'):
                service.updater.start(self.path.rsplit('/', 1)[1])
            elif self.path == '/api/update/install':
                with service.lock:
                    if service.busy:
                        raise ValueError('网络恢复正在进行，请稍后安装')
                    service.updater.install(running=service.running)
            elif self.path == "/api/start":
                service.start()
            elif self.path == "/api/check":
                service.start(once=True)
            elif self.path == "/api/stop":
                service.stop()
            elif self.path == "/api/balance/refresh":
                service.refresh_balance()
            elif self.path == "/api/credentials/forget":
                service.forget()
            elif self.path == "/api/probes/browse":
                self.send(200, {"candidates": service.browse()})
                service.audit(operation)
                return
            elif self.path in ('/api/proxy/policy','/api/proxy/group','/api/proxy/restore','/api/proxy/check','/api/proxy/client','/api/proxy/controller'):
                service.proxy_action(self.path.rsplit('/',1)[1], raw)
            elif self.path in ("/api/probes/add", "/api/probes/remove", "/api/probes/clear", "/api/probes/elevate"):
                service.probe_action(self.path.rsplit("/", 1)[1], raw)
            elif self.path == "/api/logs/clear":
                service.clear_logs()
            elif self.path == "/api/logs/open":
                service.open_logs()
            else:
                self.send(404, {"error": "操作不存在"})
                return
            service.audit(operation)
            self.send(200, {"ok": True})
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.send(400, {"error": "JSON 格式不正确"})
        except ValueError as exc:
            self.server.service.audit(operation, False)
            # ValueErrors are controlled validation messages, never raw input.
            self.send(400, {"error": str(exc) if not str(exc).startswith("invalid literal") else "请求长度格式不正确"})
        except Exception:
            self.server.service.audit(operation, False)
            self.send(500, {"error": "操作未完成，请稍后重试"})

