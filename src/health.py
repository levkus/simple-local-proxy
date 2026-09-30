#!/usr/bin/env python3
"""Connectivity check for the configured upstreams.

Answers the question the menubar can't: *does this proxy actually work right
now?* An upstream can accept the TCP connection and still be useless — the
CONNECT can be rejected, the tunnel can lead nowhere, or the destination can
refuse the exit IP it lands on. So the check does the whole round trip the
relay would do: open the tunnel, speak TLS to the destination through it, and
read a real HTTP status back.

Three outcomes, because "works" isn't binary here:
  ok    tunnel opened and the destination answered
  warn  tunnel opened, but the destination refused this exit (e.g. 403 on a
        geo-blocked IP) — the proxy is alive, it just doesn't get you in
  fail   no usable tunnel
"""

import socket
import ssl
import time
from concurrent.futures import ThreadPoolExecutor
from typing import NamedTuple

from proxy_relay import ProxyConfigError, open_upstream_tunnel, parse_upstream

# What a working proxy has to reach. This tool exists to keep the Claude CLI
# online, so that is what we probe.
CHECK_HOST = "api.anthropic.com"
CHECK_PORT = 443
CHECK_PATH = "/v1/messages"
CHECK_TIMEOUT = 12
MAX_PARALLEL = 8


class Result(NamedTuple):
    name: str
    status: str   # "ok" | "warn" | "fail"
    detail: str   # short enough for a menu item
    ms: int


def _short_error(exc):
    """Compress an exception into something that fits in a menu item."""
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, ConnectionRefusedError):
        return "refused"
    if isinstance(exc, socket.gaierror):
        return "unknown host"
    if isinstance(exc, ssl.SSLError):
        return "TLS error"
    if isinstance(exc, ProxyConfigError):
        return "bad URL"
    text = str(exc) or type(exc).__name__
    return text[:40]


class _InnerTLS:
    """A TLS session spoken *through* an already-TLS socket.

    `wrap_socket` re-wraps the raw file descriptor, so layering it over an
    SSLSocket sends the inner handshake around the outer session instead of
    inside it — the peer sees garbage and answers UNEXPECTED_MESSAGE. That is
    exactly the shape of an `https://` upstream: TLS to the proxy, then TLS to
    the destination within the tunnel. A memory BIO hands us the handshake bytes
    so we can push them through the outer socket ourselves.

    Only what the probe needs: a request out, a status line back.
    """

    def __init__(self, sock, host):
        self._sock = sock
        self._incoming, self._outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
        self._tls = ssl.create_default_context().wrap_bio(
            self._incoming, self._outgoing, server_hostname=host
        )
        self._pump(self._tls.do_handshake)

    def _pump(self, step):
        """Run one TLS operation, ferrying bytes through the outer socket."""
        while True:
            try:
                result = step()
            except (ssl.SSLWantReadError, ssl.SSLWantWriteError):
                self._flush()
                chunk = self._sock.recv(65536)
                if chunk:
                    self._incoming.write(chunk)
                else:
                    self._incoming.write_eof()
                continue
            self._flush()
            return result

    def _flush(self):
        pending = self._outgoing.read()
        if pending:
            self._sock.sendall(pending)

    def sendall(self, data):
        self._pump(lambda: self._tls.write(data))

    def recv(self, size):
        try:
            return self._pump(lambda: self._tls.read(size))
        except ssl.SSLZeroReturnError:
            return b""

    def settimeout(self, timeout):
        self._sock.settimeout(timeout)


def _probe(sock, host, use_tls):
    """Send one request down an open tunnel and return the HTTP status code."""
    if use_tls:
        sock = (
            _InnerTLS(sock, host)
            if isinstance(sock, ssl.SSLSocket)
            else ssl.create_default_context().wrap_socket(sock, server_hostname=host)
        )
    sock.sendall(
        f"GET {CHECK_PATH} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode()
    )
    sock.settimeout(CHECK_TIMEOUT)
    line = sock.recv(200).split(b"\r\n")[0].decode("latin1", "replace")
    parts = line.split(" ")
    return parts[1] if len(parts) > 1 else "000"


def check_entry(entry, target=(CHECK_HOST, CHECK_PORT), use_tls=True, timeout=CHECK_TIMEOUT):
    """Run one upstream end to end. Never raises — failures come back as Results."""
    name = entry.get("name", "?")
    host, port = target
    started = time.monotonic()
    sock = None
    try:
        up = parse_upstream(entry.get("url", ""))
        if up is None:
            sock = socket.create_connection((host, port), timeout=timeout)
        else:
            sock, _ = open_upstream_tunnel(
                up, host, port, bool(entry.get("insecure")), timeout=timeout
            )
        code = _probe(sock, host, use_tls)
    except Exception as e:  # any failure is just a red mark, never a crash
        return Result(name, "fail", _short_error(e), int((time.monotonic() - started) * 1000))
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    ms = int((time.monotonic() - started) * 1000)
    if code.startswith(("4", "5")) and code not in ("400", "401", "404", "405"):
        # Reached the destination, but it turned us away — 403 on a blocked
        # region is the case this exists for.
        return Result(name, "warn", f"HTTP {code}", ms)
    return Result(name, "ok", f"{ms} ms", ms)


def check_all(proxies, **kwargs):
    """Check every entry in parallel. Returns {name: Result}, input order preserved."""
    proxies = list(proxies)
    if not proxies:
        return {}
    with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL, len(proxies))) as pool:
        results = pool.map(lambda entry: check_entry(entry, **kwargs), proxies)
    return {r.name: r for r in results}
