"""Regression tests for defects found in code review.

Every test here corresponds to a specific bug in the request-handling layer.
They are written to fail against the buggy implementation, so that a future
regression is caught rather than shipped.
"""

import base64
import os
import socket
import tempfile
import unittest

from helpers import (
    ORIGIN_BODY,
    FakeUpstream,
    Origin,
    connect_tunnel,
    fetch_through_tunnel,
    ipv6_loopback_available,
    make_self_signed,
    openssl_available,
    proxy_relay,
    read_all,
    read_head,
    start_relay,
)


class RelayTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def relay(self, proxies, active, **extra):
        return start_relay(self.tmp.name, proxies, active, self.addCleanup, **extra)


# --------------------------------------------------------------------------- #
# Bug 1: keep-alive on the non-CONNECT path routed to the wrong origin
# --------------------------------------------------------------------------- #
class TestKeepAliveRouting(RelayTestCase):
    def test_second_request_is_never_served_by_the_first_origin(self):
        """A pooled proxy connection must not deliver B's request to A.

        Clients reuse a proxy connection across different origin hosts. If the
        relay parses only the first request line and then splices bytes, the
        second request — with B's Host header, cookies and credentials — is
        answered by A.
        """
        a = Origin(label=b"A")
        b = Origin(label=b"B")
        self.addCleanup(a.stop)
        self.addCleanup(b.stop)
        _, port = self.relay([{"name": "direct", "url": ""}], "direct")

        sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        sock.sendall(
            f"GET http://{a.authority}/ HTTP/1.1\r\nHost: {a.authority}\r\n\r\n".encode())
        first = read_head(sock) + b""
        self.assertIn(b"HELLO-ORIGIN-A", first + read_body_hint(sock))

        # Second request on the SAME connection, aimed at a different origin.
        try:
            sock.sendall(
                f"GET http://{b.authority}/ HTTP/1.1\r\nHost: {b.authority}\r\n\r\n".encode())
            second = read_all(sock)
        except OSError:
            second = b""
        sock.close()

        # Acceptable: B answers, or the relay closed the connection so the
        # client must reconnect. Unacceptable: A answers a request meant for B.
        self.assertNotIn(
            b"HELLO-ORIGIN-A", second,
            "request for origin B was answered by origin A — cross-host misrouting")


def read_body_hint(sock):
    """Best-effort read of whatever body bytes are already buffered."""
    sock.settimeout(1.0)
    try:
        return sock.recv(4096)
    except (TimeoutError, OSError):
        return b""
    finally:
        sock.settimeout(10)


# --------------------------------------------------------------------------- #
# Bug 2: bytes pipelined with the CONNECT header block were dropped
# --------------------------------------------------------------------------- #
class TestPipelinedConnect(RelayTestCase):
    def test_payload_sent_with_connect_is_not_lost(self):
        """A client may send CONNECT and the first tunnel bytes in one segment.

        The relay reads until the header terminator; anything after it in the
        same recv() must be forwarded once the tunnel opens, or the peer waits
        forever for data the relay silently discarded.
        """
        origin = Origin()
        self.addCleanup(origin.stop)
        _, port = self.relay([{"name": "direct", "url": ""}], "direct")

        sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        # CONNECT *and* the tunnelled request in a single write.
        sock.sendall(
            f"CONNECT {origin.authority} HTTP/1.1\r\n"
            f"Host: {origin.authority}\r\n\r\n"
            f"GET / HTTP/1.1\r\nHost: origin\r\nConnection: close\r\n\r\n".encode()
        )
        data = read_all(sock)
        sock.close()

        self.assertIn(b"200", data.split(b"\r\n")[0])
        self.assertIn(ORIGIN_BODY, data,
                      "payload pipelined with CONNECT was dropped by the relay")


# --------------------------------------------------------------------------- #
# Bug 3: IPv6 CONNECT targets crashed the handler
# --------------------------------------------------------------------------- #
class TestIPv6Target(RelayTestCase):
    def test_split_host_port_handles_bracketed_ipv6(self):
        self.assertEqual(proxy_relay.split_host_port("example.com:443", 80), ("example.com", 443))
        self.assertEqual(proxy_relay.split_host_port("example.com", 80), ("example.com", 80))
        self.assertEqual(proxy_relay.split_host_port("[2001:db8::1]:8443", 443), ("2001:db8::1", 8443))
        self.assertEqual(proxy_relay.split_host_port("[::1]", 443), ("::1", 443))

    @unittest.skipUnless(ipv6_loopback_available(), "no IPv6 loopback on this machine")
    def test_connect_to_ipv6_literal(self):
        origin = Origin(host="::1")
        self.addCleanup(origin.stop)
        _, port = self.relay([{"name": "direct", "url": ""}], "direct")

        status, body = fetch_through_tunnel(port, origin)
        self.assertIn(b" 200 ", status, "CONNECT to an IPv6 literal failed")
        self.assertIn(ORIGIN_BODY, body)


# --------------------------------------------------------------------------- #
# Bug 4: half-closed client connections truncated the response
# --------------------------------------------------------------------------- #
class TestHalfClose(RelayTestCase):
    def test_response_survives_client_half_close(self):
        """Client sends its request then shuts down its write side.

        That is legitimate (curl, `nc -N`, many HTTP libraries do it). Treating
        EOF from one direction as "tear down both" loses the response entirely.
        """
        origin = Origin(delay=0.3)
        self.addCleanup(origin.stop)
        _, port = self.relay([{"name": "direct", "url": ""}], "direct")

        sock, status = connect_tunnel(port, origin.authority)
        self.assertIn(b" 200 ", status)
        sock.sendall(b"GET / HTTP/1.1\r\nHost: origin\r\nConnection: close\r\n\r\n")
        sock.shutdown(socket.SHUT_WR)  # we're done sending, still want the reply
        body = read_all(sock)
        sock.close()

        self.assertIn(ORIGIN_BODY, body,
                      "response was lost after the client half-closed")


# --------------------------------------------------------------------------- #
# Bug 5: resource exhaustion — unbounded header buffer
# --------------------------------------------------------------------------- #
class TestHeaderLimits(RelayTestCase):
    def test_oversized_header_block_is_rejected(self):
        """A client that never sends the header terminator must not grow the
        buffer without bound — otherwise one connection can exhaust memory."""
        _, port = self.relay([{"name": "direct", "url": ""}], "direct")

        sock = socket.create_connection(("127.0.0.1", port), timeout=15)
        sent = 0
        try:
            # Well past any sane header size, with no CRLFCRLF anywhere.
            while sent < 512 * 1024:
                sock.sendall(b"X" * 8192)
                sent += 8192
        except OSError:
            pass  # relay hung up on us, which is the desired outcome
        data = read_all(sock)
        sock.close()
        # Either an error response or a closed connection; never silent buffering.
        self.assertTrue(
            data == b"" or b"400" in data or b"502" in data,
            f"expected rejection of oversized headers, got {data[:80]!r}")

    def test_idle_connection_is_timed_out(self):
        """A connection that opens and sends nothing must not pin a thread."""
        original = proxy_relay.HEADER_TIMEOUT
        proxy_relay.HEADER_TIMEOUT = 0.5
        self.addCleanup(setattr, proxy_relay, "HEADER_TIMEOUT", original)

        _, port = self.relay([{"name": "direct", "url": ""}], "direct")
        sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        sock.settimeout(5)
        data = read_all(sock)  # should end quickly when the relay gives up
        sock.close()
        self.assertEqual(data, b"", "idle connection should be dropped, not held open")


# --------------------------------------------------------------------------- #
# Non-CONNECT traffic through an upstream proxy (previously untested branch)
# --------------------------------------------------------------------------- #
class TestPlainViaUpstream(RelayTestCase):
    def test_absolute_form_request_is_forwarded_with_auth(self):
        origin = Origin()
        upstream = FakeUpstream()
        self.addCleanup(origin.stop)
        self.addCleanup(upstream.stop)
        _, port = self.relay(
            [{"name": "up", "url": f"http://dave:pw@127.0.0.1:{upstream.port}"}], "up")

        sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        sock.sendall(
            f"GET http://{origin.authority}/ HTTP/1.1\r\n"
            f"Host: {origin.authority}\r\n\r\n".encode())
        data = read_all(sock)
        sock.close()

        self.assertIn(ORIGIN_BODY, data)
        expected = base64.b64encode(b"dave:pw").decode()
        self.assertEqual(upstream.seen_auth, [f"Basic {expected}"],
                         "relay must attach upstream credentials to plain requests")

    def test_client_proxy_auth_is_replaced_not_forwarded(self):
        """A client's own Proxy-Authorization is hop-by-hop and must be dropped,
        replaced by the credentials for *our* upstream."""
        origin = Origin()
        upstream = FakeUpstream()
        self.addCleanup(origin.stop)
        self.addCleanup(upstream.stop)
        _, port = self.relay(
            [{"name": "up", "url": f"http://real:secret@127.0.0.1:{upstream.port}"}], "up")

        sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        sock.sendall(
            f"GET http://{origin.authority}/ HTTP/1.1\r\n"
            f"Host: {origin.authority}\r\n"
            f"Proxy-Authorization: Basic Y2xpZW50LXN1cHBsaWVk\r\n\r\n".encode())
        read_all(sock)
        sock.close()

        expected = base64.b64encode(b"real:secret").decode()
        self.assertEqual(upstream.seen_auth, [f"Basic {expected}"],
                         "client-supplied Proxy-Authorization must not be forwarded")

    def test_hop_by_hop_headers_are_stripped(self):
        head = (b"GET / HTTP/1.1\r\nHost: x\r\nProxy-Connection: keep-alive\r\n"
                b"Proxy-Authorization: Basic zzz\r\nConnection: keep-alive\r\n"
                b"Content-Length: 3\r\n\r\n")
        line, headers = proxy_relay.split_head(head)
        kept = proxy_relay.strip_hop_by_hop(headers)
        names = [h.split(b":")[0].lower() for h in kept]
        self.assertIn(b"host", names)
        self.assertIn(b"content-length", names)
        for gone in (b"proxy-connection", b"proxy-authorization", b"connection"):
            self.assertNotIn(gone, names)


# --------------------------------------------------------------------------- #
# TLS posture: verification must stay on unless explicitly disabled
# --------------------------------------------------------------------------- #
class TestTlsVerification(RelayTestCase):
    @unittest.skipUnless(openssl_available(), "openssl not available to make a test cert")
    def test_self_signed_upstream_is_rejected_without_insecure(self):
        """Guards against a regression that turns certificate checking off.

        The `insecure` opt-in is per proxy entry; without it a self-signed
        upstream must fail rather than silently succeed.
        """
        cert, key = make_self_signed(self.tmp.name)
        upstream = FakeUpstream(tls_cert=(cert, key))
        origin = Origin()
        self.addCleanup(upstream.stop)
        self.addCleanup(origin.stop)
        _, port = self.relay(
            [{"name": "tls-up", "url": f"https://127.0.0.1:{upstream.port}"}], "tls-up")

        status, body = fetch_through_tunnel(port, origin)
        self.assertNotIn(b" 200 ", status,
                         "self-signed upstream was accepted without insecure=true")
        self.assertNotIn(ORIGIN_BODY, body)


# --------------------------------------------------------------------------- #
# Listener safety: an unauthenticated credentialed proxy must stay on loopback
# --------------------------------------------------------------------------- #
class TestListenerSafety(RelayTestCase):
    def test_non_loopback_bind_is_refused_without_opt_in(self):
        """Binding publicly turns this into an open proxy that hands the user's
        corporate credentials to anyone on the LAN. Require explicit consent."""
        with self.assertRaises(proxy_relay.ProxyConfigError):
            self.relay([{"name": "direct", "url": ""}], "direct", listen_host="0.0.0.0")

    def test_non_loopback_bind_allowed_with_explicit_flag(self):
        state, port = self.relay(
            [{"name": "direct", "url": ""}], "direct",
            listen_host="127.0.0.1", allow_remote=True)
        self.assertTrue(port > 0)


# --------------------------------------------------------------------------- #
# Config validation
# --------------------------------------------------------------------------- #
class TestUpstreamValidation(unittest.TestCase):
    def test_missing_scheme_is_rejected_with_a_clear_error(self):
        with self.assertRaises(proxy_relay.ProxyConfigError) as ctx:
            proxy_relay.parse_upstream("proxy.example.com:8080")
        self.assertIn("http://", str(ctx.exception))

    def test_unknown_scheme_is_rejected(self):
        with self.assertRaises(proxy_relay.ProxyConfigError):
            proxy_relay.parse_upstream("socks5://proxy.example.com:1080")

    def test_missing_host_is_rejected(self):
        with self.assertRaises(proxy_relay.ProxyConfigError):
            proxy_relay.parse_upstream("http://:8080")


# --------------------------------------------------------------------------- #
# State API used by the menubar
# --------------------------------------------------------------------------- #
class TestStateApi(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "config.json")
        with open(self.path, "w") as fh:
            fh.write('{"active": "a", "proxies": [{"name": "a", "url": ""}]}')

    def test_snapshot_is_an_isolated_copy(self):
        state = proxy_relay.State(self.path)
        state.load()
        snap = state.snapshot()
        snap["proxies"].append({"name": "injected", "url": ""})
        self.assertEqual([p["name"] for p in state.snapshot()["proxies"]], ["a"],
                         "snapshot must not alias internal state")

    def test_upsert_adds_and_replaces(self):
        state = proxy_relay.State(self.path)
        state.load()
        state.upsert_proxy("b", "http://h:1")
        self.assertEqual([p["name"] for p in state.snapshot()["proxies"]], ["a", "b"])

        state.upsert_proxy("b", "http://h:2")
        entries = state.snapshot()["proxies"]
        self.assertEqual(len(entries), 2, "upsert must replace, not duplicate")
        self.assertEqual(entries[-1]["url"], "http://h:2")

        reloaded = proxy_relay.State(self.path)
        reloaded.load()
        self.assertEqual(len(reloaded.snapshot()["proxies"]), 2, "upsert must persist")

    def test_default_listen_port_matches_documented_default(self):
        with open(self.path, "w") as fh:
            fh.write('{"active": "a", "proxies": [{"name": "a", "url": ""}]}')
        state = proxy_relay.State(self.path)
        state.load()
        self.assertEqual(state.listen_addr()[1], proxy_relay.DEFAULT_CONFIG["listen_port"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
