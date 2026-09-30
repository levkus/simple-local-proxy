"""Shared test scaffolding: throwaway servers and a relay harness.

Everything binds to loopback on an ephemeral port and writes config into a temp
directory, so tests never touch a real ~/.proxy-relay or the network.
"""

import itertools
import json
import os
import socket
import ssl
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import proxy_relay  # noqa: E402

ORIGIN_BODY = b"HELLO-ORIGIN"
_counter = itertools.count()


# --------------------------------------------------------------------------- #
# Socket utilities
# --------------------------------------------------------------------------- #
def read_head(sock):
    """Read up to and including the end of an HTTP header block."""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
    return buf


def read_all(sock):
    out = b""
    while True:
        try:
            chunk = sock.recv(4096)
        except OSError:
            break
        if not chunk:
            break
        out += chunk
    return out


def pipe(a, b):
    """Bidirectional splice; returns once both directions are finished."""

    def one(src, dst):
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        finally:
            try:
                dst.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    t = threading.Thread(target=one, args=(a, b), daemon=True)
    t.start()
    one(b, a)
    t.join(timeout=5)
    for sock in (a, b):
        try:
            sock.close()
        except OSError:
            pass


def split_authority(authority, default_port):
    """Split 'host:port' / '[v6]:port' — local copy so helpers stay independent."""
    authority = authority.strip()
    if authority.startswith("["):
        host, sep, rest = authority[1:].partition("]")
        if not sep:
            raise ValueError(f"malformed authority: {authority!r}")
        port = rest[1:] if rest.startswith(":") else ""
        return host, int(port or default_port)
    if authority.count(":") > 1:
        return authority, default_port
    host, _, port = authority.partition(":")
    return host, int(port or default_port)


def ipv6_loopback_available():
    if not socket.has_ipv6:
        return False
    try:
        s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        try:
            s.bind(("::1", 0))
            return True
        finally:
            s.close()
    except OSError:
        return False


def openssl_available():
    try:
        subprocess.run(["openssl", "version"], check=True, capture_output=True)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


def make_self_signed(tmpdir):
    cert = os.path.join(tmpdir, "cert.pem")
    key = os.path.join(tmpdir, "key.pem")
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048",
            "-keyout", key, "-out", cert, "-days", "1", "-nodes",
            "-subj", "/CN=127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )
    return cert, key


# --------------------------------------------------------------------------- #
# Fake servers
# --------------------------------------------------------------------------- #
class Origin:
    """An HTTP server that echoes which instance answered.

    `label` lets a test tell two origins apart, which is how we detect requests
    being delivered to the wrong host.
    """

    def __init__(self, label=b"", host="127.0.0.1", delay=0.0, status=200, tls_cert=None):
        body = ORIGIN_BODY + (b"-" + label if label else b"")

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):  # noqa: N802
                if delay:
                    threading.Event().wait(delay)
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                # Honour the client's wish to close; otherwise keep alive.
                if self.headers.get("Connection", "").lower() == "close":
                    self.send_header("Connection", "close")
                    self.close_connection = True
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        server_cls = ThreadingHTTPServer
        if ":" in host:
            class _V6(ThreadingHTTPServer):
                address_family = socket.AF_INET6

            server_cls = _V6

        self.body = body
        self.server = server_cls((host, 0), _Handler)
        if tls_cert:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(tls_cert[0], tls_cert[1])
            self.server.socket = ctx.wrap_socket(self.server.socket, server_side=True)
        self.host = host
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def authority(self):
        return f"[{self.host}]:{self.port}" if ":" in self.host else f"{self.host}:{self.port}"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class FakeUpstream:
    """A minimal upstream proxy that records what the relay sent it.

    Speaks CONNECT (tunnelling to the real target) and absolute-form requests.
    Optionally wraps itself in TLS, standing in for an `https://` proxy.
    """

    def __init__(self, tls_cert=None, reject_with=None):
        self.tls_cert = tls_cert          # (certfile, keyfile) or None
        self.reject_with = reject_with    # e.g. b"HTTP/1.1 407 ...\r\n\r\n"
        self.seen_auth = []               # Proxy-Authorization values observed
        self.seen_targets = []            # request lines observed
        self.seen_heads = []              # full header blocks observed
        self._closed = False

        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while not self._closed:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        try:
            if self.tls_cert:
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                ctx.load_cert_chain(self.tls_cert[0], self.tls_cert[1])
                conn = ctx.wrap_socket(conn, server_side=True)

            head = read_head(conn)
            if not head:
                return
            self.seen_heads.append(head)
            lines = head.split(b"\r\n")
            request_line = lines[0].decode("latin1")
            self.seen_targets.append(request_line)
            for line in lines[1:]:
                if line.lower().startswith(b"proxy-authorization:"):
                    self.seen_auth.append(line.split(b":", 1)[1].strip().decode("latin1"))

            if self.reject_with:
                conn.sendall(self.reject_with)
                return

            parts = request_line.split()
            if parts[0].upper() == "CONNECT":
                host, port = split_authority(parts[1], 443)
                target = socket.create_connection((host, port), timeout=10)
                conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                pipe(conn, target)
            else:
                from urllib.parse import urlsplit

                u = urlsplit(parts[1])
                target = socket.create_connection((u.hostname, u.port or 80), timeout=10)
                path = u.path or "/"
                rebuilt = head.replace(
                    f"{parts[0]} {parts[1]} {parts[2]}".encode(),
                    f"{parts[0]} {path} {parts[2]}".encode(),
                    1,
                )
                target.sendall(rebuilt)
                pipe(conn, target)
        except (OSError, ssl.SSLError, IndexError, ValueError):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def stop(self):
        self._closed = True
        try:
            self.sock.close()
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# Relay harness
# --------------------------------------------------------------------------- #
def start_relay(tmpdir, proxies, active, cleanup, **extra):
    """Boot a relay on an ephemeral port with its own State. Returns (state, port)."""
    path = os.path.join(tmpdir, f"config-{next(_counter)}.json")
    config = {
        "listen_host": "127.0.0.1",
        "listen_port": 0,
        "active": active,
        "proxies": proxies,
    }
    config.update(extra)
    with open(path, "w") as fh:
        json.dump(config, fh)

    state = proxy_relay.State(path)
    state.load()
    server = proxy_relay.start_server(state)
    cleanup(server.server_close)
    cleanup(server.shutdown)
    return state, server.server_address[1]


def connect_tunnel(relay_port, authority, timeout=10):
    """Open a CONNECT tunnel through the relay. Returns (socket, status_line)."""
    sock = socket.create_connection(("127.0.0.1", relay_port), timeout=timeout)
    sock.sendall(f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n".encode())
    head = read_head(sock)
    return sock, head.split(b"\r\n")[0]


def fetch_through_tunnel(relay_port, origin):
    """CONNECT to `origin` via the relay, then GET / down the tunnel."""
    sock, status = connect_tunnel(relay_port, origin.authority)
    if b" 200 " not in status:
        sock.close()
        return status, b""
    sock.sendall(b"GET / HTTP/1.1\r\nHost: origin\r\nConnection: close\r\n\r\n")
    body = read_all(sock)
    sock.close()
    return status, body
