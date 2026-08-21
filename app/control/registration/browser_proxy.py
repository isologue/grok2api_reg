"""Browser proxy parsing and Chromium authentication support.

Chromium's ``--proxy-server`` switch does not reliably consume proxy URL
userinfo. Keep the endpoint in the switch and answer proxy authentication
challenges from a short-lived MV3 extension instead.
"""
from __future__ import annotations

import json
import select
import shutil
import socket
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

_SUPPORTED_SCHEMES = {"http", "https", "socks4", "socks4a", "socks5", "socks5h", "socks"}
_CHROMIUM_SCHEMES = {"socks": "socks5", "socks5h": "socks5", "socks4a": "socks4"}


@dataclass(frozen=True, slots=True)
class BrowserProxy:
    """A validated browser proxy with credentials kept out of logs/CLI args."""

    scheme: str
    host: str
    port: int
    username: str = ""
    password: str = ""

    @property
    def has_auth(self) -> bool:
        return bool(self.username or self.password)

    @property
    def chromium_scheme(self) -> str:
        return _CHROMIUM_SCHEMES.get(self.scheme, self.scheme)

    @property
    def chromium_url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host and not self.host.startswith("[") else self.host
        return f"{self.chromium_scheme}://{host}:{self.port}"

    @property
    def log_label(self) -> str:
        # Never include the username or password: proxy credentials are
        # secrets and registration logs are persisted on disk.
        return f"{self.chromium_scheme}://{self.host}:{self.port}"


def parse_browser_proxy(value: str | None) -> BrowserProxy | None:
    """Parse ``scheme://[user:pass@]host:port`` for the registration browser.

    ``socks5h``/``socks4a`` are accepted for parity with the API proxy
    settings and are mapped to Chromium's ``socks5``/``socks4`` schemes.
    Bare ``host:port`` values remain supported as HTTP proxies.
    """

    raw = str(value or "").strip()
    if not raw:
        return None
    candidate = raw if "://" in raw else f"http://{raw}"
    try:
        parsed = urlsplit(candidate)
    except ValueError as exc:
        raise ValueError("浏览器代理格式无效，请填写 scheme://[账号:密码@]host:port") from exc
    scheme = (parsed.scheme or "http").lower()
    if scheme not in _SUPPORTED_SCHEMES:
        raise ValueError(f"浏览器代理协议不支持：{scheme}，支持 http、https、socks4、socks5")
    host = parsed.hostname or ""
    if not host:
        raise ValueError("浏览器代理缺少主机地址")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("浏览器代理端口无效") from exc
    if port is None:
        port = 443 if scheme == "https" else 80
    if not 1 <= port <= 65535:
        raise ValueError("浏览器代理端口必须在 1 到 65535 之间")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("浏览器代理不能包含路径、查询参数或片段")
    username = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    return BrowserProxy(scheme, host, port, username, password)


def _read_exact(sock: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        part = sock.recv(size - len(data))
        if not part:
            raise ConnectionError("SOCKS connection closed unexpectedly")
        data.extend(part)
    return bytes(data)


def _read_socks_reply(sock: socket.socket) -> bytes:
    header = _read_exact(sock, 4)
    atyp = header[3]
    if atyp == 1:
        suffix = _read_exact(sock, 6)
    elif atyp == 4:
        suffix = _read_exact(sock, 18)
    elif atyp == 3:
        length = _read_exact(sock, 1)
        suffix = length + _read_exact(sock, length[0] + 2)
    else:
        raise ConnectionError("SOCKS server returned an unknown address type")
    return header + suffix


class Socks5AuthenticatedBridge:
    """Expose a local unauthenticated SOCKS5 endpoint for Chromium.

    Chromium can use an unauthenticated SOCKS5 endpoint but cannot reliably
    provide RFC 1929 username/password credentials to a remote SOCKS server.
    This narrow bridge performs that upstream authentication and relays only
    CONNECT streams from the registration browser. It remains process-local
    and is removed when the registration worker exits.
    """

    def __init__(self, upstream: BrowserProxy) -> None:
        if upstream.chromium_scheme != "socks5" or not upstream.has_auth:
            raise ValueError("SOCKS5 认证转发仅适用于带账号密码的 SOCKS5 代理")
        self._upstream = upstream
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._port = 0

    def start(self) -> str:
        if self._listener:
            return self.chromium_url
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(16)
        listener.settimeout(0.5)
        self._listener = listener
        self._port = int(listener.getsockname()[1])
        self._thread = threading.Thread(target=self._accept_loop, name="registration-socks-bridge", daemon=True)
        self._thread.start()
        return self.chromium_url

    @property
    def chromium_url(self) -> str:
        if not self._port:
            raise RuntimeError("SOCKS5 认证转发尚未启动")
        return f"socks5://127.0.0.1:{self._port}"

    def close(self) -> None:
        self._stop.set()
        listener, self._listener = self._listener, None
        if listener:
            try:
                listener.close()
            except OSError:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1)
        self._thread = None

    def _accept_loop(self) -> None:
        listener = self._listener
        if not listener:
            return
        while not self._stop.is_set():
            try:
                client, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            threading.Thread(target=self._handle_client, args=(client,), name="registration-socks-client", daemon=True).start()

    def _handle_client(self, client: socket.socket) -> None:
        upstream: socket.socket | None = None
        try:
            client.settimeout(15)
            greeting = _read_exact(client, 2)
            if greeting[0] != 5:
                return
            methods = _read_exact(client, greeting[1])
            if 0 not in methods:
                client.sendall(b"\x05\xff")
                return
            client.sendall(b"\x05\x00")

            request_head = _read_exact(client, 4)
            if request_head[0] != 5 or request_head[1] != 1:
                client.sendall(b"\x05\x07\x00\x01\x00\x00\x00\x00\x00\x00")
                return
            atyp = request_head[3]
            if atyp == 1:
                request_tail = _read_exact(client, 6)
            elif atyp == 4:
                request_tail = _read_exact(client, 18)
            elif atyp == 3:
                length = _read_exact(client, 1)
                request_tail = length + _read_exact(client, length[0] + 2)
            else:
                client.sendall(b"\x05\x08\x00\x01\x00\x00\x00\x00\x00\x00")
                return

            upstream = socket.create_connection((self._upstream.host, self._upstream.port), timeout=15)
            upstream.settimeout(15)
            username = self._upstream.username.encode("utf-8")
            password = self._upstream.password.encode("utf-8")
            if len(username) > 255 or len(password) > 255:
                raise ValueError("SOCKS5 代理账号或密码不能超过 255 字节")
            upstream.sendall(b"\x05\x01\x02")
            if _read_exact(upstream, 2) != b"\x05\x02":
                raise ConnectionError("上游 SOCKS5 代理不支持账号密码认证")
            upstream.sendall(b"\x01" + bytes((len(username),)) + username + bytes((len(password),)) + password)
            if _read_exact(upstream, 2) != b"\x01\x00":
                raise PermissionError("上游 SOCKS5 代理账号或密码验证失败")
            upstream.sendall(request_head + request_tail)
            reply = _read_socks_reply(upstream)
            client.sendall(reply)
            if reply[1] != 0:
                return
            client.settimeout(None)
            upstream.settimeout(None)
            self._relay(client, upstream)
        except (ConnectionError, OSError, PermissionError, ValueError):
            try:
                client.sendall(b"\x05\x01\x00\x01\x00\x00\x00\x00\x00\x00")
            except OSError:
                pass
        finally:
            for sock in (upstream, client):
                if sock:
                    try:
                        sock.close()
                    except OSError:
                        pass

    @staticmethod
    def _relay(left: socket.socket, right: socket.socket) -> None:
        sockets = [left, right]
        while True:
            readable, _, exceptional = select.select(sockets, [], sockets, 1)
            if exceptional:
                return
            for source in readable:
                data = source.recv(65536)
                if not data:
                    return
                (right if source is left else left).sendall(data)


def create_proxy_auth_extension(proxy: BrowserProxy) -> str:
    """Create a short-lived MV3 extension answering proxy auth challenges."""

    if not proxy.has_auth:
        return ""
    directory = Path(tempfile.mkdtemp(prefix="grok-register-proxy-"))
    manifest = {
        "manifest_version": 3,
        "name": "Registration Proxy Authentication",
        "version": "1.0.0",
        "permissions": ["webRequest", "webRequestAuthProvider"],
        "host_permissions": ["<all_urls>"],
        "background": {"service_worker": "background.js"},
    }
    background = f"""
const username = {json.dumps(proxy.username, ensure_ascii=False)};
const password = {json.dumps(proxy.password, ensure_ascii=False)};
const attempts = new Map();

chrome.webRequest.onAuthRequired.addListener((details, respond) => {{
  if (!details.isProxy) {{
    respond({{}});
    return;
  }}
  const count = (attempts.get(details.requestId) || 0) + 1;
  attempts.set(details.requestId, count);
  if (count > 3) {{
    respond({{cancel: true}});
    return;
  }}
  respond({{authCredentials: {{username, password}}}});
}}, {{urls: ["<all_urls>"]}}, ["asyncBlocking"]);

const forget = (details) => attempts.delete(details.requestId);
chrome.webRequest.onCompleted.addListener(forget, {{urls: ["<all_urls>"]}});
chrome.webRequest.onErrorOccurred.addListener(forget, {{urls: ["<all_urls>"]}});
""".strip() + "\n"
    (directory / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (directory / "background.js").write_text(background, encoding="utf-8")
    return str(directory)


def remove_proxy_auth_extension(directory: str | None) -> None:
    if directory:
        shutil.rmtree(directory, ignore_errors=True)
