# Changelog

## Unreleased

### Fixed

- **Check connection reported every `https://` upstream as a TLS error**, even
  a working one. The probe raised its TLS session with `wrap_socket()`, which
  re-wraps the raw file descriptor; over the `SSLSocket` of an `https://` proxy
  that sends the inner handshake around the outer session instead of through
  it. The inner session now runs over a memory BIO.
- **Requests on a pooled connection could be answered by the wrong origin.**
  The non-CONNECT path parsed only the first request line and then spliced raw
  bytes, so a client reusing one proxy connection for a second host had that
  request — along with its `Host`, cookies and `Authorization` — delivered to
  the first host. Each request is now handled on its own connection.
- **Bytes sent in the same packet as `CONNECT` were dropped**, which hung
  tunnels whose client pipelined the TLS ClientHello behind the header block.
- **IPv6 destinations failed** with a `ValueError` — `host:port` splitting did
  not understand `[::1]:443`.
- **A client half-close discarded the response.** Sending a request and then
  shutting down the write side (curl, `nc -N`, several HTTP libraries) tore down
  the upstream socket before the reply arrived.
- Hop-by-hop headers (`Connection`, `Proxy-Connection`, `Proxy-Authorization`,
  `Keep-Alive`, `Upgrade`) are now stripped instead of forwarded to the origin.
- `install.sh` aborts on a malformed `config.json` instead of continuing and
  leaving the alias port and the relay port disagreeing, and it writes the port
  back atomically so a crash cannot truncate the file holding your credentials.
- The default `listen_port` fallback in code said `8888` while the documented
  default was `17872`.

### Added

- **`packaging/build-app.sh`** builds a self-contained `ProxyRelay.app` with its
  own Python inside, so the relay survives losing the Python it was installed
  against — a managed Mac can lose Homebrew, and with it brew's Python, leaving
  the LaunchAgent pointing at an interpreter that no longer exists. `--install`
  also repoints the LaunchAgent at the bundle. `config.json` keeps living in
  `~/.proxy-relay`, since a signed bundle can't be written to.
- **`packaging/make-dmg.sh`** packages the bundle into a drag-to-install disk
  image for handing to other people. Ad-hoc by default, with a note in the image
  explaining the Gatekeeper warning that unsigned downloads get; set
  `CODESIGN_IDENTITY` and `NOTARY_PROFILE` to sign and notarise instead, which
  removes the warning entirely. Images are per-architecture and named
  accordingly — a build only runs on the architecture it was built for.
- **Check connection** in the menubar: probes every configured upstream in
  parallel and marks each one in place — `✓` with the round-trip time, `!` when
  the tunnel opens but the destination refuses that exit IP (a geo-block reads
  very differently from a dead proxy), `✗` with a short reason otherwise. The
  probe is the whole round trip the relay would make, not a TCP connect, so an
  upstream that accepts connections and then leads nowhere is not reported as
  healthy.
- Read timeout and a 64 KB header cap, so an idle or malicious connection can no
  longer pin a thread forever or grow the buffer without bound.
- The relay refuses to bind a non-loopback address unless `"allow_remote": true`
  is set: unauthenticated *and* credential-attaching, a public bind would be an
  open proxy for the whole network.
- `parse_upstream` rejects URLs with no scheme or host, with a message naming
  the fix, rather than failing later as an opaque 502.
- Tests: 42 Python tests (including regression tests for every bug above) plus
  22 end-to-end checks of `install.sh` / `uninstall.sh` against a throwaway HOME.
- `requirements.txt` pins `rumps`; ruff config for linting.

### Changed

- `State` takes its config path and exposes `snapshot()` / `upsert_proxy()`, so
  the menubar no longer reaches into private internals or mutates config under
  its own lock. `start_server(state)` takes the state instead of a global.
- "Reload config" now re-applies the icon too, which the README already claimed.
- `menubar.py` opens the config with `subprocess.run` instead of `os.system`,
  removing a quoting hazard on paths containing shell metacharacters.
- The default relay port is **17872** (was 13546). Existing installs keep their
  port: re-running `install.sh` defaults to whatever is already in your config.
