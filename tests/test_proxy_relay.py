"""Core tests: upstream parsing, auth headers, config state, and real traffic.

Hermetic — everything runs on 127.0.0.1 against throwaway servers and temp
config files. Nothing touches a real ~/.proxy-relay, LaunchAgent, or network.
Regression tests for specific review findings live in test_request_handling.py.
"""

import base64
import json
import os
import socket
import tempfile
import unittest

from helpers import (
    ORIGIN_BODY,
    FakeUpstream,
    Origin,
    fetch_through_tunnel,
    make_self_signed,
    openssl_available,
    proxy_relay,
    read_all,
    read_head,
    start_relay,
)


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

    def test_ipv6_upstream_literal(self):
        up = proxy_relay.parse_upstream("http://[2001:db8::1]:8080")
        self.assertEqual(up["host"], "2001:db8::1")
        self.assertEqual(up["port"], 8080)


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

    def write(self, cfg):
        with open(self.path, "w") as fh:
            json.dump(cfg, fh)

    def state(self):
        state = proxy_relay.State(self.path)
        state.load()
        return state

    def test_loads_active_proxy(self):
        self.write({
            "listen_port": 1234,
            "active": "b",
            "proxies": [{"name": "a", "url": ""}, {"name": "b", "url": "http://h:1"}],
        })
        state = self.state()
        self.assertEqual(state.current()["name"], "b")
        self.assertEqual(state.listen_addr(), ("127.0.0.1", 1234))

    def test_set_active_switches_and_persists(self):
        self.write({
            "active": "a",
            "proxies": [{"name": "a", "url": ""}, {"name": "b", "url": "http://h:1"}],
        })
        state = self.state()
        state.set_active("b")
        self.assertEqual(state.current()["name"], "b")

        # a fresh State reading the same file must see the change
        reloaded = proxy_relay.State(self.path)
        reloaded.load()
        self.assertEqual(reloaded.current()["name"], "b")

    def test_unknown_active_resolves_to_nothing(self):
        self.write({"active": "missing", "proxies": [{"name": "a", "url": ""}]})
        self.assertIsNone(self.state().current())

    def test_saved_config_is_not_world_readable(self):
        self.write({"active": "a", "proxies": [{"name": "a", "url": ""}]})
        state = self.state()
        state.set_active("a")
        mode = os.stat(self.path).st_mode & 0o777
        self.assertEqual(mode, 0o600, "config holds credentials; must stay chmod 600")

    def test_current_is_a_copy(self):
        self.write({"active": "a", "proxies": [{"name": "a", "url": "http://h:1"}]})
        state = self.state()
        got = state.current()
        got["url"] = "http://tampered:1"
        self.assertEqual(state.current()["url"], "http://h:1")


# --------------------------------------------------------------------------- #
# Integration: real bytes through the relay
# --------------------------------------------------------------------------- #
class TestRelayTraffic(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.origin = Origin()
        self.addCleanup(self.origin.stop)

    def relay(self, proxies, active):
        return start_relay(self.tmp.name, proxies, active, self.addCleanup)

    # --- direct ------------------------------------------------------------ #
    def test_direct_connect_tunnel(self):
        _, port = self.relay([{"name": "direct", "url": ""}], "direct")
        status, body = fetch_through_tunnel(port, self.origin)
        self.assertIn(b" 200 ", status)
        self.assertIn(ORIGIN_BODY, body)

    def test_direct_plain_http_request(self):
        """Absolute-form GET (no CONNECT) gets rewritten and forwarded."""
        _, port = self.relay([{"name": "direct", "url": ""}], "direct")
        sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        sock.sendall(
            f"GET http://{self.origin.authority}/ HTTP/1.1\r\n"
            f"Host: {self.origin.authority}\r\n\r\n".encode()
        )
        self.assertIn(ORIGIN_BODY, read_all(sock))
        sock.close()

    # --- via an HTTP upstream proxy ---------------------------------------- #
    def test_connect_through_http_upstream_with_auth(self):
        upstream = FakeUpstream()
        self.addCleanup(upstream.stop)
        _, port = self.relay(
            [{"name": "up", "url": f"http://alice:s3cret@127.0.0.1:{upstream.port}"}], "up")

        status, body = fetch_through_tunnel(port, self.origin)
        self.assertIn(b" 200 ", status)
        self.assertIn(ORIGIN_BODY, body)

        # the upstream must have been used, with correct credentials
        self.assertTrue(any("CONNECT" in t for t in upstream.seen_targets))
        expected = base64.b64encode(b"alice:s3cret").decode()
        self.assertEqual(upstream.seen_auth, [f"Basic {expected}"])

    def test_upstream_rejection_surfaces_as_502(self):
        upstream = FakeUpstream(reject_with=b"HTTP/1.1 407 Proxy Auth Required\r\n\r\n")
        self.addCleanup(upstream.stop)
        _, port = self.relay(
            [{"name": "up", "url": f"http://127.0.0.1:{upstream.port}"}], "up")

        sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        sock.sendall(f"CONNECT {self.origin.authority} HTTP/1.1\r\n\r\n".encode())
        head = read_head(sock)
        sock.close()
        self.assertIn(b"502", head.split(b"\r\n")[0])

    # --- via an HTTPS (TLS) upstream proxy --------------------------------- #
    @unittest.skipUnless(openssl_available(), "openssl not available to make a test cert")
    def test_connect_through_https_upstream(self):
        cert, key = make_self_signed(self.tmp.name)
        upstream = FakeUpstream(tls_cert=(cert, key))
        self.addCleanup(upstream.stop)
        _, port = self.relay(
            [{
                "name": "tls-up",
                "url": f"https://bob:hunter2@127.0.0.1:{upstream.port}",
                "insecure": True,  # self-signed cert in the test
            }],
            "tls-up",
        )

        status, body = fetch_through_tunnel(port, self.origin)
        self.assertIn(b" 200 ", status)
        self.assertIn(ORIGIN_BODY, body)
        expected = base64.b64encode(b"bob:hunter2").decode()
        self.assertEqual(upstream.seen_auth, [f"Basic {expected}"])

    # --- the headline feature: switching without restarting ---------------- #
    def test_switching_upstream_takes_effect_on_next_connection(self):
        upstream = FakeUpstream()
        self.addCleanup(upstream.stop)
        state, port = self.relay(
            [
                {"name": "direct", "url": ""},
                {"name": "up", "url": f"http://carol:pw@127.0.0.1:{upstream.port}"},
            ],
            "direct",
        )

        # first request: direct, upstream untouched
        _, body = fetch_through_tunnel(port, self.origin)
        self.assertIn(ORIGIN_BODY, body)
        self.assertEqual(upstream.seen_targets, [])

        # flip the active upstream, exactly like clicking the menubar
        state.set_active("up")

        # second request: same relay, same port, now via the upstream
        _, body = fetch_through_tunnel(port, self.origin)
        self.assertIn(ORIGIN_BODY, body)
        self.assertTrue(any("CONNECT" in t for t in upstream.seen_targets))

        # and back again
        state.set_active("direct")
        before = len(upstream.seen_targets)
        _, body = fetch_through_tunnel(port, self.origin)
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

    def test_example_config_ships_no_credentials(self):
        """The example must carry placeholders, never a real secret."""
        path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "config.example.json")
        with open(path) as fh:
            cfg = json.load(fh)
        for entry in cfg["proxies"]:
            up = proxy_relay.parse_upstream(entry["url"])
            if up and up["pw"]:
                self.assertEqual(
                    up["pw"], "PASSWORD",
                    "config.example.json must use a placeholder password")


if __name__ == "__main__":
    unittest.main(verbosity=2)
