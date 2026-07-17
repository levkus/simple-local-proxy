#!/usr/bin/env python3
"""Local proxy relay with a macOS menubar switcher.

Clients point HTTP(S)_PROXY at this relay on 127.0.0.1:<listen_port> (default
13546). The relay tunnels each new connection through whichever upstream proxy
is currently selected in the menubar. Switching the upstream never touches the
client -- it keeps talking to the relay the whole time; only *new* connections
use the newly selected upstream.

Supported upstream kinds (per entry in config.json):
  - ""                              -> direct (no upstream proxy)
  - "http://[user:pass@]host:port"  -> plain HTTP proxy
  - "https://[user:pass@]host:port" -> HTTPS proxy (TLS-to-proxy)

Run headless (no menubar) with:  python3 proxy_relay.py --headless
"""

import base64
import json
import os
import select
import socket
import socketserver
import ssl
import sys
import threading
import urllib.parse

# Config lives next to this script, or wherever PROXY_RELAY_HOME points.
APP_DIR = os.environ.get("PROXY_RELAY_HOME") or os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")

DEFAULT_CONFIG = {
    "listen_host": "127.0.0.1",
    "listen_port": 13546,
    "active": "direct",
    "proxies": [
        {"name": "direct", "url": ""},
    ],
}


# --------------------------------------------------------------------------- #
# Shared state
# --------------------------------------------------------------------------- #
class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.config = dict(DEFAULT_CONFIG)
        self.active = None  # resolved active proxy dict

    def _resolve(self):
        name = self.config.get("active")
        self.active = None
        for p in self.config.get("proxies", []):
            if p.get("name") == name:
                self.active = p
                return

    def load(self):
        with self.lock:
            with open(CONFIG_PATH) as f:
                self.config = json.load(f)
            self._resolve()

    def save_locked(self):
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.config, f, indent=2)
        os.replace(tmp, CONFIG_PATH)
        try:
            os.chmod(CONFIG_PATH, 0o600)
        except OSError:
            pass

    def set_active(self, name):
        with self.lock:
            self.config["active"] = name
            self._resolve()
            self.save_locked()

    def current(self):
        with self.lock:
            return dict(self.active) if self.active else None

    def listen_addr(self):
        with self.lock:
            return (
                self.config.get("listen_host", "127.0.0.1"),
                int(self.config.get("listen_port", 8888)),
            )


STATE = State()


# --------------------------------------------------------------------------- #
# Upstream helpers
# --------------------------------------------------------------------------- #
def parse_upstream(url):
    """Return dict describing the upstream proxy, or None for direct."""
    if not url:
        return None
    u = urllib.parse.urlsplit(url)
    scheme = (u.scheme or "http").lower()
    return {
        "scheme": scheme,
        "host": u.hostname,
        "port": u.port or (443 if scheme == "https" else 80),
        "user": urllib.parse.unquote(u.username) if u.username else None,
        "pw": urllib.parse.unquote(u.password) if u.password else None,
    }


def _wrap_tls(sock, host, insecure):
    ctx = ssl.create_default_context()
    if insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx.wrap_socket(sock, server_hostname=host)


def _auth_header(up):
    if up.get("user") is None:
        return b""
    token = base64.b64encode(
        f"{up['user']}:{up.get('pw') or ''}".encode()
    ).decode()
    return f"Proxy-Authorization: Basic {token}\r\n".encode()


def open_upstream_tunnel(up, host, port, insecure):
    """CONNECT to host:port through the upstream proxy. Returns (sock, leftover)."""
    sock = socket.create_connection((up["host"], up["port"]), timeout=30)
    try:
        if up["scheme"] == "https":
            sock = _wrap_tls(sock, up["host"], insecure)
        req = (
            f"CONNECT {host}:{port} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
        ).encode() + _auth_header(up) + b"\r\n"
        sock.sendall(req)

        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
        head, _, leftover = buf.partition(b"\r\n\r\n")
        status_line = head.split(b"\r\n", 1)[0].decode("latin1", "replace")
        parts = status_line.split(" ")
        code = parts[1] if len(parts) > 1 else "000"
        if not code.startswith("2"):
            raise IOError(f"upstream CONNECT rejected: {status_line!r}")
        return sock, leftover
    except Exception:
        sock.close()
        raise


# --------------------------------------------------------------------------- #
# Byte pump
# --------------------------------------------------------------------------- #
def pump(s1, s2):
    socks = [s1, s2]
    try:
        while True:
            r, _, x = select.select(socks, [], socks)
            if x:
                break
            for s in r:
                other = s2 if s is s1 else s1
                data = s.recv(65536)
                if not data:
                    return
                # drain any TLS-buffered application data
                if isinstance(s, ssl.SSLSocket):
                    while s.pending():
                        more = s.recv(65536)
                        if not more:
                            break
                        data += more
                other.sendall(data)
    except OSError:
        return
    finally:
        for s in socks:
            try:
                s.close()
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# Request handler
# --------------------------------------------------------------------------- #
class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        client = self.request
        try:
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = client.recv(4096)
                if not chunk:
                    return
                head += chunk
            request_line = head.split(b"\r\n", 1)[0].decode("latin1", "replace")
            parts = request_line.split()
            if len(parts) < 3:
                return
            method, target, version = parts[0], parts[1], parts[2]
            if method.upper() == "CONNECT":
                self._connect(client, target)
            else:
                self._plain(client, method, target, version, head)
        except Exception as e:
            sys.stderr.write(f"[relay] error: {e}\n")
            try:
                client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            except OSError:
                pass

    def _active(self):
        active = STATE.current() or {}
        return parse_upstream(active.get("url", "")), bool(active.get("insecure"))

    def _connect(self, client, target):
        host, _, port = target.partition(":")
        port = int(port or 443)
        up, insecure = self._active()
        if up is None:
            remote = socket.create_connection((host, port), timeout=30)
            leftover = b""
        else:
            remote, leftover = open_upstream_tunnel(up, host, port, insecure)
        client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        if leftover:
            client.sendall(leftover)
        pump(client, remote)

    def _plain(self, client, method, target, version, head):
        up, insecure = self._active()
        if up is not None and up["scheme"] in ("http", "https"):
            remote = socket.create_connection((up["host"], up["port"]), timeout=30)
            if up["scheme"] == "https":
                remote = _wrap_tls(remote, up["host"], insecure)
            out = head
            if up.get("user") is not None and b"proxy-authorization" not in head.lower():
                out = head.replace(b"\r\n\r\n", b"\r\n" + _auth_header(up) + b"\r\n", 1)
            remote.sendall(out)
            pump(client, remote)
        else:
            u = urllib.parse.urlsplit(target)
            host = u.hostname
            port = u.port or 80
            path = urllib.parse.urlunsplit(("", "", u.path or "/", u.query, "")) or "/"
            out = head.replace(
                f"{method} {target} {version}".encode(),
                f"{method} {path} {version}".encode(),
                1,
            )
            remote = socket.create_connection((host, port), timeout=30)
            remote.sendall(out)
            pump(client, remote)


class ThreadingTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


# --------------------------------------------------------------------------- #
# Bootstrap
# --------------------------------------------------------------------------- #
def ensure_config():
    os.makedirs(APP_DIR, exist_ok=True)
    if not os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        os.chmod(CONFIG_PATH, 0o600)


def start_server():
    host, port = STATE.listen_addr()
    server = ThreadingTCPServer((host, port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    sys.stderr.write(f"[relay] listening on {host}:{port}\n")
    return server


def main():
    ensure_config()
    STATE.load()
    start_server()

    if "--headless" in sys.argv:
        threading.Event().wait()
        return

    from menubar import RelayApp

    RelayApp().run()


if __name__ == "__main__":
    main()
