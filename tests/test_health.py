"""Tests for the upstream connectivity check behind the menubar's Check connection.

Hermetic: every probe targets a throwaway Origin on loopback, never the real
CHECK_HOST. Most cases run the probe without TLS — the fake origins speak plain
HTTP, and the transport is the same. TestTlsProbe covers the one case where it
is not: an `https://` upstream, where the probe's own TLS has to ride inside the
TLS already spoken to the proxy.
"""

import os
import socket
import ssl
import sys
import tempfile
import unittest
from unittest import mock

from helpers import FakeUpstream, Origin, make_self_signed, openssl_available

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import health  # noqa: E402


def closed_port():
    """A port nothing listens on: bind, read the number, let it go."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class HealthTestCase(unittest.TestCase):
    def check(self, entry, origin, **kwargs):
        return health.check_entry(
            entry, target=(origin.host, origin.port), use_tls=False, timeout=5, **kwargs
        )


class TestCheckEntry(HealthTestCase):
    def setUp(self):
        self.origin = Origin()
        self.addCleanup(self.origin.stop)

    def test_working_http_proxy_is_ok(self):
        up = FakeUpstream()
        self.addCleanup(up.stop)
        res = self.check({"name": "work", "url": f"http://127.0.0.1:{up.port}"}, self.origin)
        self.assertEqual(res.status, "ok")
        self.assertEqual(res.name, "work")
        self.assertIn("ms", res.detail)

    def test_direct_entry_is_checked_without_a_proxy(self):
        res = self.check({"name": "direct", "url": ""}, self.origin)
        self.assertEqual(res.status, "ok")

    def test_credentials_reach_the_upstream(self):
        up = FakeUpstream()
        self.addCleanup(up.stop)
        res = self.check(
            {"name": "auth", "url": f"http://alice:s3cret@127.0.0.1:{up.port}"}, self.origin
        )
        self.assertEqual(res.status, "ok")
        self.assertEqual(up.seen_auth, ["Basic YWxpY2U6czNjcmV0"])

    def test_rejected_connect_is_a_failure(self):
        up = FakeUpstream(reject_with=b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n")
        self.addCleanup(up.stop)
        res = self.check({"name": "needs-auth", "url": f"http://127.0.0.1:{up.port}"}, self.origin)
        self.assertEqual(res.status, "fail")
        self.assertIn("407", res.detail)

    def test_dead_proxy_is_a_failure(self):
        res = self.check({"name": "dead", "url": f"http://127.0.0.1:{closed_port()}"}, self.origin)
        self.assertEqual(res.status, "fail")
        self.assertEqual(res.detail, "refused")

    def test_unusable_url_is_a_failure_not_a_crash(self):
        res = self.check({"name": "typo", "url": "ftp://127.0.0.1:21"}, self.origin)
        self.assertEqual(res.status, "fail")
        self.assertEqual(res.detail, "bad URL")

    def test_result_carries_elapsed_milliseconds(self):
        res = self.check({"name": "direct", "url": ""}, self.origin)
        self.assertGreaterEqual(res.ms, 0)


class TestRefusedDestination(HealthTestCase):
    """The proxy works, the destination turns us away — that is a warning, not a failure."""

    def test_forbidden_destination_is_a_warning(self):
        origin = Origin(status=403)
        self.addCleanup(origin.stop)
        up = FakeUpstream()
        self.addCleanup(up.stop)
        res = self.check({"name": "geo", "url": f"http://127.0.0.1:{up.port}"}, origin)
        self.assertEqual(res.status, "warn")
        self.assertEqual(res.detail, "HTTP 403")

    def test_auth_required_from_the_destination_still_counts_as_reachable(self):
        # 401 means we got all the way to the API — it just wants credentials,
        # which the probe deliberately does not send.
        origin = Origin(status=401)
        self.addCleanup(origin.stop)
        res = self.check({"name": "direct", "url": ""}, origin)
        self.assertEqual(res.status, "ok")


@unittest.skipUnless(openssl_available(), "needs openssl to mint a throwaway cert")
class TestTlsProbe(unittest.TestCase):
    """An `https://` upstream means two TLS layers: to the proxy, then inside the
    tunnel to the destination. The inner one cannot be a plain wrap_socket() of
    the outer SSLSocket, so this is the case that catches getting it wrong."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cert = make_self_signed(tmp.name)
        # The probe's inner context has to trust a throwaway cert on an IP, which
        # a default context never will — that check is not what these test.
        unverified = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        unverified.check_hostname = False
        unverified.verify_mode = ssl.CERT_NONE
        patch = mock.patch.object(health.ssl, "create_default_context", lambda: unverified)
        patch.start()
        self.addCleanup(patch.stop)

    def test_https_upstream_reaches_a_tls_destination(self):
        origin = Origin(tls_cert=self.cert)
        self.addCleanup(origin.stop)
        up = FakeUpstream(tls_cert=self.cert)
        self.addCleanup(up.stop)
        res = health.check_entry(
            {"name": "tls-proxy", "url": f"https://127.0.0.1:{up.port}", "insecure": True},
            target=(origin.host, origin.port),
            use_tls=True,
            timeout=5,
        )
        self.assertEqual(res.status, "ok", f"expected a working upstream, got {res.detail!r}")

    def test_http_upstream_reaches_a_tls_destination(self):
        origin = Origin(tls_cert=self.cert)
        self.addCleanup(origin.stop)
        up = FakeUpstream()
        self.addCleanup(up.stop)
        res = health.check_entry(
            {"name": "plain-proxy", "url": f"http://127.0.0.1:{up.port}"},
            target=(origin.host, origin.port),
            use_tls=True,
            timeout=5,
        )
        self.assertEqual(res.status, "ok", f"expected a working upstream, got {res.detail!r}")


class TestCheckAll(HealthTestCase):
    def test_every_entry_comes_back_keyed_by_name(self):
        origin = Origin()
        self.addCleanup(origin.stop)
        up = FakeUpstream()
        self.addCleanup(up.stop)
        results = health.check_all(
            [
                {"name": "good", "url": f"http://127.0.0.1:{up.port}"},
                {"name": "dead", "url": f"http://127.0.0.1:{closed_port()}"},
                {"name": "direct", "url": ""},
            ],
            target=(origin.host, origin.port),
            use_tls=False,
            timeout=5,
        )
        self.assertEqual(
            {name: r.status for name, r in results.items()},
            {"good": "ok", "dead": "fail", "direct": "ok"},
        )

    def test_empty_list_is_handled(self):
        self.assertEqual(health.check_all([]), {})


if __name__ == "__main__":
    unittest.main()
