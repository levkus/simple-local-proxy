#!/usr/bin/env python3
"""Local proxy relay with a macOS menubar switcher.

Clients point HTTP(S)_PROXY at this relay on 127.0.0.1:<listen_port> (default
17872). The relay tunnels each new connection through whichever upstream proxy
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
import ipaddress
import json
import os
import select
import socket
import socketserver
import ssl
import sys
import threading
import urllib.parse


def default_app_dir():
    """Where config.json lives when PROXY_RELAY_HOME doesn't say otherwise.

    Next to this script normally. A frozen .app bundle is read-only and
    code-signed, so writing the config inside it would fail and break the
    signature — those fall back to the installer's directory.
    """
    if getattr(sys, "frozen", False):
        return os.path.expanduser("~/.proxy-relay")
    return os.path.dirname(os.path.abspath(__file__))


APP_DIR = os.environ.get("PROXY_RELAY_HOME") or default_app_dir()
CONFIG_PATH = os.path.join(APP_DIR, "config.json")

DEFAULT_CONFIG = {
    "listen_host": "127.0.0.1",
    "listen_port": 17872,
    "active": "direct",
    "proxies": [
        {"name": "direct", "url": ""},
    ],
}

# A client that opens a connection and then says nothing must not pin a thread
# for the life of the process, and a client that never terminates its header
# block must not grow the buffer without bound.
HEADER_TIMEOUT = 30          # seconds to wait for the request header block
MAX_HEADER_BYTES = 64 * 1024
CONNECT_TIMEOUT = 30

# Headers that apply to a single hop and must not be passed along (RFC 7230 6.1).
HOP_BY_HOP = (
    b"connection",
    b"proxy-connection",
    b"keep-alive",
    b"proxy-authorization",
    b"upgrade",
)


class ProxyConfigError(ValueError):
    """Raised for configuration that cannot be honoured safely."""


# --------------------------------------------------------------------------- #
# Shared state
# --------------------------------------------------------------------------- #
class State:
    """The active-upstream selection, backed by a JSON file.

    A single instance is shared by the relay threads and the menubar. All
    mutation goes through methods that hold the lock and persist atomically, so
    callers never need to touch the internals.
    """

    def __init__(self, config_path=None):
        self.lock = threading.Lock()
        self.config_path = config_path or CONFIG_PATH
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
            with open(self.config_path) as f:
                self.config = json.load(f)
            self._resolve()

    def save_locked(self):
        """Persist atomically. Caller must hold the lock."""
        tmp = self.config_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.config, f, indent=2)
        os.replace(tmp, self.config_path)
        try:
            os.chmod(self.config_path, 0o600)
        except OSError:
            pass

    def set_active(self, name):
        with self.lock:
            self.config["active"] = name
            self._resolve()
            self.save_locked()

    def upsert_proxy(self, name, url, insecure=False):
        """Add a proxy, or replace the entry with the same name."""
        with self.lock:
            proxies = [p for p in self.config.get("proxies", []) if p.get("name") != name]
            entry = {"name": name, "url": url}
            if insecure:
                entry["insecure"] = True
            proxies.append(entry)
            self.config["proxies"] = proxies
            self._resolve()
            self.save_locked()

    def current(self):
        with self.lock:
            return dict(self.active) if self.active else None

    def snapshot(self):
        """An isolated deep copy of the config, safe to read without the lock."""
        with self.lock:
            return json.loads(json.dumps(self.config))

    def listen_addr(self):
        with self.lock:
            return (
                self.config.get("listen_host", DEFAULT_CONFIG["listen_host"]),
                int(self.config.get("listen_port", DEFAULT_CONFIG["listen_port"])),
            )


STATE = State()


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #
def parse_upstream(url):
    """Return a dict describing the upstream proxy, or None for direct.

    Raises ProxyConfigError for URLs we cannot act on, so the failure surfaces
    with a message the user can act on instead of an opaque connection error.
    """
    if not url:
        return None
    u = urllib.parse.urlsplit(url)
    scheme = (u.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise ProxyConfigError(
            f"upstream URL must start with http:// or https:// — got {url!r}")
    if not u.hostname:
        raise ProxyConfigError(f"upstream URL has no host — got {url!r}")
    return {
        "scheme": scheme,
        "host": u.hostname,
        "port": u.port or (443 if scheme == "https" else 80),
        "user": urllib.parse.unquote(u.username) if u.username else None,
        "pw": urllib.parse.unquote(u.password) if u.password else None,
    }


def split_host_port(authority, default_port):
    """Split an authority into (host, port), handling IPv6 literals.

    'example.com:443' -> ('example.com', 443)
    '[2001:db8::1]:8443' -> ('2001:db8::1', 8443)
    """
    authority = authority.strip()
    if authority.startswith("["):
        host, sep, rest = authority[1:].partition("]")
        if not sep or not host:
            raise ValueError(f"malformed authority: {authority!r}")
        port = rest[1:] if rest.startswith(":") else ""
        return host, int(port or default_port)
    if authority.count(":") > 1:
        return authority, default_port  # bare IPv6 literal, no port
    host, _, port = authority.partition(":")
    if not host:
        raise ValueError(f"malformed authority: {authority!r}")
    return host, int(port or default_port)


def split_head(head):
    """Split a header block into (request_line, [header lines])."""
    block = head.split(b"\r\n\r\n", 1)[0]
    lines = block.split(b"\r\n")
    return lines[0], [line for line in lines[1:] if line]


def join_head(request_line, headers):
    return b"\r\n".join([request_line] + headers) + b"\r\n\r\n"


def strip_hop_by_hop(headers):
    """Drop headers that must not be forwarded to the next hop."""
    kept = []
    for header in headers:
        name = header.split(b":", 1)[0].strip().lower()
        if name in HOP_BY_HOP:
            continue
        kept.append(header)
    return kept


# --------------------------------------------------------------------------- #
# Upstream helpers
# --------------------------------------------------------------------------- #
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


def open_upstream_tunnel(up, host, port, insecure, timeout=CONNECT_TIMEOUT):
    """CONNECT to host:port through the upstream proxy. Returns (sock, leftover)."""
    sock = socket.create_connection((up["host"], up["port"]), timeout=timeout)
    try:
        if up["scheme"] == "https":
            sock = _wrap_tls(sock, up["host"], insecure)
        authority = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        req = (
            f"CONNECT {authority} HTTP/1.1\r\n"
            f"Host: {authority}\r\n"
        ).encode() + _auth_header(up) + b"\r\n"
        sock.sendall(req)

        buf = b""
        while b"\r\n\r\n" not in buf:
            if len(buf) > MAX_HEADER_BYTES:
                raise OSError("upstream sent an oversized response header block")
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
        head, _, leftover = buf.partition(b"\r\n\r\n")
        status_line = head.split(b"\r\n", 1)[0].decode("latin1", "replace")
        parts = status_line.split(" ")
        code = parts[1] if len(parts) > 1 else "000"
        if not code.startswith("2"):
            raise OSError(f"upstream CONNECT rejected: {status_line!r}")
        return sock, leftover
    except Exception:
        sock.close()
        raise


# --------------------------------------------------------------------------- #
# Byte pump
# --------------------------------------------------------------------------- #
def pump(a, b):
    """Splice two sockets until both directions are done.

    EOF on one side only closes that direction: the peer is told with a
    half-close and we keep reading the other way. A client that sends its
    request and then shuts down its write side still gets its response.
    """
    readable = {a, b}
    try:
        while readable:
            r, _, x = select.select(list(readable), [], list(readable))
            if x:
                break
            for sock in r:
                other = b if sock is a else a
                try:
                    data = sock.recv(65536)
                except OSError:
                    data = b""
                if data and isinstance(sock, ssl.SSLSocket):
                    # select() can't see data already buffered inside the TLS layer
                    while sock.pending():
                        more = sock.recv(65536)
                        if not more:
                            break
                        data += more
                if not data:
                    readable.discard(sock)
                    try:
                        other.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    continue
                try:
                    other.sendall(data)
                except OSError:
                    return
    except (OSError, ValueError):
        return
    finally:
        for sock in (a, b):
            try:
                sock.close()
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# Request handler
# --------------------------------------------------------------------------- #
class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        client = self.request
        try:
            raw = self._read_head(client)
        except (TimeoutError, OSError):
            return
        if not raw:
            return  # client vanished, or oversized and already answered

        head, _, leftover = raw.partition(b"\r\n\r\n")
        head += b"\r\n\r\n"
        try:
            request_line = head.split(b"\r\n", 1)[0].decode("latin1", "replace")
            parts = request_line.split()
            if len(parts) < 3:
                return
            method, target, version = parts[0], parts[1], parts[2]
            if method.upper() == "CONNECT":
                self._connect(client, target, leftover)
            else:
                self._plain(client, method, target, version, head, leftover)
        except Exception as e:
            sys.stderr.write(f"[relay] error: {e}\n")
            try:
                client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            except OSError:
                pass

    def _read_head(self, client):
        """Read the request header block, bounded in both time and size."""
        client.settimeout(HEADER_TIMEOUT)
        raw = b""
        try:
            while b"\r\n\r\n" not in raw:
                if len(raw) > MAX_HEADER_BYTES:
                    try:
                        client.sendall(
                            b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n"
                            b"Connection: close\r\n\r\n")
                    except OSError:
                        pass
                    return b""
                chunk = client.recv(4096)
                if not chunk:
                    return b""
                raw += chunk
        finally:
            try:
                client.settimeout(None)
            except OSError:
                pass
        return raw

    def _state(self):
        return getattr(self.server, "state", STATE)

    def _active(self):
        active = self._state().current() or {}
        return parse_upstream(active.get("url", "")), bool(active.get("insecure"))

    def _connect(self, client, target, leftover=b""):
        host, port = split_host_port(target, 443)
        up, insecure = self._active()
        if up is None:
            remote = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT)
            upstream_leftover = b""
        else:
            remote, upstream_leftover = open_upstream_tunnel(up, host, port, insecure)

        client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        if upstream_leftover:
            client.sendall(upstream_leftover)
        # Anything the client pipelined behind the CONNECT header block belongs
        # to the tunnel — forward it, or the peer waits for bytes we swallowed.
        if leftover:
            remote.sendall(leftover)
        pump(client, remote)

    def _plain(self, client, method, target, version, head, leftover=b""):
        """Forward one absolute-form request.

        Deliberately one request per connection: we strip keep-alive and add
        `Connection: close`. A pooled proxy connection can carry requests for
        different origins, and splicing raw bytes after parsing only the first
        request line would deliver later requests — with their Host, cookies
        and credentials — to the first origin.
        """
        up, insecure = self._active()
        request_line, headers = split_head(head)
        headers = strip_hop_by_hop(headers)
        headers.append(b"Connection: close")

        if up is not None:
            remote = socket.create_connection((up["host"], up["port"]),
                                              timeout=CONNECT_TIMEOUT)
            if up["scheme"] == "https":
                remote = _wrap_tls(remote, up["host"], insecure)
            auth = _auth_header(up)
            if auth:
                headers.append(auth.rstrip(b"\r\n"))
            out = join_head(request_line, headers)  # upstream wants absolute-form
        else:
            u = urllib.parse.urlsplit(target)
            if not u.hostname:
                raise ValueError(f"cannot route request target {target!r}")
            path = urllib.parse.urlunsplit(("", "", u.path or "/", u.query, "")) or "/"
            out = join_head(f"{method} {path} {version}".encode(), headers)
            remote = socket.create_connection((u.hostname, u.port or 80),
                                              timeout=CONNECT_TIMEOUT)

        remote.sendall(out)
        if leftover:
            remote.sendall(leftover)  # request body already read with the head
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


def _is_loopback(host):
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def start_server(state=None):
    state = state if state is not None else STATE
    host, port = state.listen_addr()

    # This listener requires no authentication and attaches the user's upstream
    # credentials to everything it forwards. Bound publicly it is an open,
    # credentialed proxy for the whole network, so require explicit consent.
    if not _is_loopback(host) and not state.snapshot().get("allow_remote"):
        raise ProxyConfigError(
            f"refusing to bind {host!r}: this relay is unauthenticated and adds your "
            "upstream proxy credentials to every request, so a non-loopback bind "
            "would expose an open credentialed proxy to the network. Set "
            '"allow_remote": true in config.json if that is really what you want.'
        )

    server = ThreadingTCPServer((host, port), Handler)
    server.state = state
    threading.Thread(target=server.serve_forever, daemon=True).start()
    # Report the address actually bound (port 0 means "pick a free one").
    bound_host, bound_port = server.server_address[:2]
    sys.stderr.write(f"[relay] listening on {bound_host}:{bound_port}\n")
    return server


def main():
    ensure_config()
    STATE.load()
    start_server(STATE)

    if "--headless" in sys.argv:
        threading.Event().wait()
        return

    from menubar import RelayApp

    RelayApp().run()


if __name__ == "__main__":
    main()
