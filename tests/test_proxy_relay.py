"""Tests for the proxy relay core.

These are hermetic: everything runs on 127.0.0.1 against throwaway servers and
temp config files. Nothing touches your real ~/.proxy-relay, your LaunchAgent,
or the network.

Layout:
  * unit tests      — upstream URL parsing and Proxy-Authorization headers
  * state tests     — config load/save and switching the active upstream
  * integration     — real traffic pushed through the relay: direct, via an
                      HTTP upstream proxy, via an HTTPS (TLS) upstream proxy,
                      plus live switching and upstream failure handling
"""

import base64
import json
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import proxy_relay  # noqa: E402

ORIGIN_BODY = b"HELLO-ORIGIN"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def read_head(sock):
    """Read until the end of HTTP headers."""
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
    """Bidirectional splice, returns when either side closes."""

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


class Origin:
    """A plain HTTP server that answers every GET with ORIGIN_BODY."""

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Length", str(len(ORIGIN_BODY)))
            self.end_headers()
            self.wfile.write(ORIGIN_BODY)

        def log_message(self, *_args):
            pass

    def __init__(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class FakeUpstream:
    """A minimal upstream proxy that records what the relay sent it.

    Speaks CONNECT (tunnelling to the real target) and absolute-form requests.
    Optionally wraps itself in TLS, to stand in for an `https://` proxy.
    """

    def __init__(self, tls_cert=None, reject_with=None):
        self.tls_cert = tls_cert          # (certfile, keyfile) or None
        self.reject_with = reject_with    # e.g. b"HTTP/1.1 407 ..." to refuse
        self.seen_auth = []               # Proxy-Authorization values observed
        self.seen_targets = []            # CONNECT targets / request lines
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
            lines = head.split(b"\r\n")
            request_line = lines[0].decode("latin1")
            self.seen_targets.append(request_line)
            for line in lines[1:]:
                if line.lower().startswith(b"proxy-authorization:"):
                    self.seen_auth.append(line.split(b":", 1)[1].strip().decode("latin1"))

            if self.reject_with:
                conn.sendall(self.reject_with)
                conn.close()
                return

            parts = request_line.split()
            if parts[0].upper() == "CONNECT":
                host, _, port = parts[1].partition(":")
                target = socket.create_connection((host, int(port)), timeout=10)
                conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                pipe(conn, target)
            else:
                # absolute-form request: forward it on to the origin
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
        except (OSError, ssl.SSLError, IndexError):
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
# Unit: upstream URL parsing
# --------------------------------------------------------------------------- #
class TestParseUpstream(unittest.TestCase):
    def test_empty_url_means_direct(self):
        self.assertIsNone(proxy_relay.parse_upstream(""))
        self.assertIsNone(proxy_relay.parse_upstream(None))

    def test_http_defaults_to_port_80(self):
        up = proxy_relay.parse_upstream("http://proxy.example.com")
        self.assertEqual(up["scheme"], "http")
        self.assertEqual(up["host"], "proxy.example.com")
        self.assertEqual(up["port"], 80)
        self.assertIsNone(up["user"])

    def test_https_defaults_to_port_443(self):
        up = proxy_relay.parse_upstream("https://proxy.example.com")
        self.assertEqual(up["scheme"], "https")
        self.assertEqual(up["port"], 443)

    def test_explicit_port_wins(self):
        up = proxy_relay.parse_upstream("https://proxy.example.com:8443")
        self.assertEqual(up["port"], 8443)

    def test_credentials_are_extracted(self):
        up = proxy_relay.parse_upstream("http://alice:s3cret@proxy.example.com:8080")
        self.assertEqual(up["user"], "alice")
        self.assertEqual(up["pw"], "s3cret")
        self.assertEqual(up["host"], "proxy.example.com")

    def test_percent_encoded_credentials_are_decoded(self):
        # Passwords containing @ or : must be percent-encoded in the URL.
        up = proxy_relay.parse_upstream("http://user%40corp:p%40ss%3Aword@proxy.example.com:8080")
        self.assertEqual(up["user"], "user@corp")
        self.assertEqual(up["pw"], "p@ss:word")


class TestAuthHeader(unittest.TestCase):
    def test_no_credentials_means_no_header(self):
        up = proxy_relay.parse_upstream("http://proxy.example.com")
        self.assertEqual(proxy_relay._auth_header(up), b"")

    def test_basic_auth_is_base64_encoded(self):
        up = proxy_relay.parse_upstream("http://alice:s3cret@proxy.example.com")
        expected = base64.b64encode(b"alice:s3cret").decode()
        self.assertEqual(
            proxy_relay._auth_header(up),
            f"Proxy-Authorization: Basic {expected}\r\n".encode(),
        )

    def test_empty_password_is_allowed(self):
        up = proxy_relay.parse_upstream("http://alice@proxy.example.com")
        expected = base64.b64encode(b"alice:").decode()
        self.assertIn(expected.encode(), proxy_relay._auth_header(up))


# --------------------------------------------------------------------------- #
# State: config + switching
# --------------------------------------------------------------------------- #
class TestState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "config.json")
        self._orig_config_path = proxy_relay.CONFIG_PATH
        proxy_relay.CONFIG_PATH = self.path
        self.addCleanup(setattr, proxy_relay, "CONFIG_PATH", self._orig_config_path)

    def write(self, cfg):
        with open(self.path, "w") as fh:
            json.dump(cfg, fh)

    def test_loads_active_proxy(self):
        self.write({
            "listen_port": 1234,
            "active": "b",
            "proxies": [{"name": "a", "url": ""}, {"name": "b", "url": "http://h:1"}],
        })
        state = proxy_relay.State()
        state.load()
        self.assertEqual(state.current()["name"], "b")
        self.assertEqual(state.listen_addr(), ("127.0.0.1", 1234))

    def test_set_active_switches_and_persists(self):
        self.write({
            "active": "a",
            "proxies": [{"name": "a", "url": ""}, {"name": "b", "url": "http://h:1"}],
        })
        state = proxy_relay.State()
        state.load()
        state.set_active("b")
        self.assertEqual(state.current()["name"], "b")

        # a fresh State reading the same file must see the change
        reloaded = proxy_relay.State()
        reloaded.load()
        self.assertEqual(reloaded.current()["name"], "b")

    def test_unknown_active_resolves_to_nothing(self):
        self.write({"active": "missing", "proxies": [{"name": "a", "url": ""}]})
        state = proxy_relay.State()
        state.load()
        self.assertIsNone(state.current())

    def test_saved_config_is_not_world_readable(self):
        self.write({"active": "a", "proxies": [{"name": "a", "url": ""}]})
        state = proxy_relay.State()
        state.load()
        state.set_active("a")
        mode = os.stat(self.path).st_mode & 0o777
        self.assertEqual(mode, 0o600, "config holds credentials; must stay chmod 600")


# --------------------------------------------------------------------------- #
# Integration: real bytes through the relay
# --------------------------------------------------------------------------- #
class TestRelayTraffic(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.origin = Origin()
        self.addCleanup(self.origin.stop)
        self._orig_config_path = proxy_relay.CONFIG_PATH
        self.addCleanup(setattr, proxy_relay, "CONFIG_PATH", self._orig_config_path)

    def start_relay(self, proxies, active):
        path = os.path.join(self.tmp.name, "config.json")
        with open(path, "w") as fh:
            json.dump(
                {"listen_host": "127.0.0.1", "listen_port": 0,
                 "active": active, "proxies": proxies}, fh)
        proxy_relay.CONFIG_PATH = path
        proxy_relay.STATE.load()
        server = proxy_relay.start_server()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def fetch_through_tunnel(self, relay_port):
        """CONNECT to the origin through the relay, then GET / down the tunnel."""
        sock = socket.create_connection(("127.0.0.1", relay_port), timeout=10)
        sock.sendall(
            f"CONNECT 127.0.0.1:{self.origin.port} HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{self.origin.port}\r\n\r\n".encode()
        )
        head = read_head(sock)
        status = head.split(b"\r\n")[0]
        if b" 200 " not in status:
            sock.close()
            return status, b""
        sock.sendall(b"GET / HTTP/1.1\r\nHost: origin\r\nConnection: close\r\n\r\n")
        body = read_all(sock)
        sock.close()
        return status, body

    # --- direct ------------------------------------------------------------ #
    def test_direct_connect_tunnel(self):
        port = self.start_relay([{"name": "direct", "url": ""}], "direct")
        status, body = self.fetch_through_tunnel(port)
        self.assertIn(b" 200 ", status)
        self.assertIn(ORIGIN_BODY, body)

    def test_direct_plain_http_request(self):
        """Absolute-form GET (no CONNECT) gets rewritten and forwarded."""
        port = self.start_relay([{"name": "direct", "url": ""}], "direct")
        sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        sock.sendall(
            f"GET http://127.0.0.1:{self.origin.port}/ HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{self.origin.port}\r\nConnection: close\r\n\r\n".encode()
        )
        self.assertIn(ORIGIN_BODY, read_all(sock))
        sock.close()

    # --- via an HTTP upstream proxy ---------------------------------------- #
    def test_connect_through_http_upstream_with_auth(self):
        upstream = FakeUpstream()
        self.addCleanup(upstream.stop)
        port = self.start_relay(
            [{"name": "up", "url": f"http://alice:s3cret@127.0.0.1:{upstream.port}"}], "up")

        status, body = self.fetch_through_tunnel(port)
        self.assertIn(b" 200 ", status)
        self.assertIn(ORIGIN_BODY, body)

        # the upstream must have been used, with correct credentials
        self.assertTrue(any("CONNECT" in t for t in upstream.seen_targets))
        expected = base64.b64encode(b"alice:s3cret").decode()
        self.assertEqual(upstream.seen_auth, [f"Basic {expected}"])

    def test_upstream_rejection_surfaces_as_502(self):
        upstream = FakeUpstream(reject_with=b"HTTP/1.1 407 Proxy Auth Required\r\n\r\n")
        self.addCleanup(upstream.stop)
        port = self.start_relay(
            [{"name": "up", "url": f"http://127.0.0.1:{upstream.port}"}], "up")

        sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        sock.sendall(
            f"CONNECT 127.0.0.1:{self.origin.port} HTTP/1.1\r\n\r\n".encode())
        head = read_head(sock)
        sock.close()
        self.assertIn(b"502", head.split(b"\r\n")[0])

    # --- via an HTTPS (TLS) upstream proxy --------------------------------- #
    @unittest.skipUnless(openssl_available(), "openssl not available to make a test cert")
    def test_connect_through_https_upstream(self):
        cert, key = make_self_signed(self.tmp.name)
        upstream = FakeUpstream(tls_cert=(cert, key))
        self.addCleanup(upstream.stop)
        port = self.start_relay(
            [{
                "name": "tls-up",
                "url": f"https://bob:hunter2@127.0.0.1:{upstream.port}",
                "insecure": True,  # self-signed cert in the test
            }],
            "tls-up",
        )

        status, body = self.fetch_through_tunnel(port)
        self.assertIn(b" 200 ", status)
        self.assertIn(ORIGIN_BODY, body)
        expected = base64.b64encode(b"bob:hunter2").decode()
        self.assertEqual(upstream.seen_auth, [f"Basic {expected}"])

    # --- the headline feature: switching without restarting ---------------- #
    def test_switching_upstream_takes_effect_on_next_connection(self):
        upstream = FakeUpstream()
        self.addCleanup(upstream.stop)
        port = self.start_relay(
            [
                {"name": "direct", "url": ""},
                {"name": "up", "url": f"http://carol:pw@127.0.0.1:{upstream.port}"},
            ],
            "direct",
        )

        # first request: direct, upstream untouched
        _, body = self.fetch_through_tunnel(port)
        self.assertIn(ORIGIN_BODY, body)
        self.assertEqual(upstream.seen_targets, [])

        # flip the active upstream, exactly like clicking the menubar
        proxy_relay.STATE.set_active("up")

        # second request: same relay, same port, now via the upstream
        _, body = self.fetch_through_tunnel(port)
        self.assertIn(ORIGIN_BODY, body)
        self.assertTrue(any("CONNECT" in t for t in upstream.seen_targets))

        # and back again
        proxy_relay.STATE.set_active("direct")
        before = len(upstream.seen_targets)
        _, body = self.fetch_through_tunnel(port)
        self.assertIn(ORIGIN_BODY, body)
        self.assertEqual(len(upstream.seen_targets), before, "should not touch upstream")


# --------------------------------------------------------------------------- #
# The shipped example config must stay valid
# --------------------------------------------------------------------------- #
class TestExampleConfig(unittest.TestCase):
    def test_example_config_is_usable(self):
        path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "config.example.json")
        with open(path) as fh:
            cfg = json.load(fh)

        self.assertIn("listen_port", cfg)
        self.assertIsInstance(cfg["listen_port"], int)
        self.assertTrue(1024 <= cfg["listen_port"] <= 65535)
        self.assertIn("proxies", cfg)
        self.assertTrue(cfg["proxies"], "should ship at least one example entry")

        names = [p["name"] for p in cfg["proxies"]]
        self.assertIn(cfg["active"], names, "active must reference an existing proxy")
        for entry in cfg["proxies"]:
            self.assertIn("name", entry)
            self.assertIn("url", entry)
            # every example URL must parse (or be the empty 'direct' one)
            proxy_relay.parse_upstream(entry["url"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
