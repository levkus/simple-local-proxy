# Menubar Proxy Switcher (macOS)

[![tests](https://github.com/levkus/simple-local-proxy/actions/workflows/tests.yml/badge.svg)](https://github.com/levkus/simple-local-proxy/actions/workflows/tests.yml)

A tiny local proxy relay with a menubar icon that lets you **switch between
upstream proxies with a click** — no restarting the app that's using the proxy.

```
  your app  ──►  relay (127.0.0.1:17872)  ──►  upstream selected in the menubar
 (CLI / GUI)      always the same address        work / backup / direct / …
```

You point a client at the relay **once** (a fixed local address). The relay
tunnels each new connection through whichever upstream you've picked in the
menubar. Flip the upstream in the menu and the client keeps talking to the same
`127.0.0.1:17872` — only *new* connections use the new upstream. Nothing to
restart.

Built for pointing the Claude CLI and the Claude desktop app at a switchable
proxy, but it's a generic HTTP/HTTPS proxy — anything that honors
`HTTP(S)_PROXY` (or Chromium's `--proxy-server`) can use it.

---

## Requirements

- macOS 11+ (SF Symbols; tested on macOS 26 Tahoe).
- **Homebrew** or **python.org** Python 3.11+. The Python bundled with Xcode /
  Command Line Tools **cannot** build the `pyobjc` dependency — the installer
  will `brew install python@3.12` for you if Homebrew is present.

## Install

```sh
git clone <this-repo-url> mac-proxy-switcher
cd mac-proxy-switcher
./install.sh
```

The installer asks which port the relay should listen on (default **17872**;
press Enter to accept). Re-running it later defaults to the port already in your
config, so upgrades never move the port behind your back. For scripted installs:

```sh
./install.sh --port 12345
```

The installer:
- copies the app into `~/.proxy-relay/` and builds a venv with `rumps`,
- seeds `~/.proxy-relay/config.json` from `config.example.json`,
- installs a **LaunchAgent** so it starts automatically at every login,
- generates the menubar icon,
- adds `claude` and `claude-app` aliases to `~/.zshrc` (rewritten on re-install
  so the port stays in sync).

Then put your real proxies in `~/.proxy-relay/config.json` (or menubar →
**Edit list…**) and hit menubar → **Reload config**.

> Credentials live only in `config.json`, which is git-ignored. Never commit it.

## Configure

`config.json`:

```json
{
  "listen_host": "127.0.0.1",
  "listen_port": 17872,
  "active": "work-https",
  "icon": { "symbol": "globe", "point": 16, "weight": "regular" },
  "proxies": [
    { "name": "work-https", "url": "https://user:pass@proxy.example.com:8443" },
    { "name": "work-http",  "url": "http://user:pass@proxy.example.com:8080" },
    { "name": "direct",     "url": "" }
  ]
}
```

Upstream `url` forms:

| Form | Meaning |
|------|---------|
| `https://user:pass@host:port` | HTTPS proxy — **TLS to the proxy itself**. Add `"insecure": true` to the entry if it uses a self-signed cert. |
| `http://user:pass@host:port`  | Plain HTTP proxy. |
| `""` (empty)                  | Direct — no upstream, connect straight out. |

Passwords containing `@` or `:` must be percent-encoded (`p@ss` → `p%40ss`).

> **`listen_host` stays on loopback.** The relay requires no authentication and
> attaches your upstream credentials to everything it forwards, so binding it to
> `0.0.0.0` would hand an open, credentialed proxy to everyone on your network.
> It refuses to start on a non-loopback address unless you also set
> `"allow_remote": true`.

`listen_port` is chosen during install (default 17872). The aliases and the
desktop launcher are generated from it. To change it later, re-run
`./install.sh` and enter the new port — it rewrites the alias block, the
launcher and the LaunchAgent to match.

## Use it

**Menubar** — click the icon:
- pick a proxy (checkmark = active; `Active: …` shows the current one),
- **Check connection** — probe every upstream and mark the results,
- **Add proxy…** — quick `name = url` entry,
- **Edit list…** — open `config.json`,
- **Reload config** — re-read the file (also re-applies the icon).

**Check connection** answers what the menu otherwise can't: does this proxy work
*right now?* Each entry gets the full round trip — open the tunnel, speak TLS to
`api.anthropic.com` through it, read a real HTTP status back — so a proxy that
accepts connections and then leads nowhere is not mistaken for a working one.
Every entry is probed in parallel; verdicts land next to the names:

| Mark | Meaning |
|------|---------|
| `✓ 351 ms` | tunnel opened, destination answered — usable |
| `! HTTP 403` | tunnel opened, destination refused this exit IP (geo-block) — the proxy is alive, it just doesn't get you in |
| `✗ timeout` | no usable tunnel (also: `refused`, `bad URL`, a rejected `CONNECT`) |

The marks are from the last check, not live — re-run it after switching networks.

**Claude CLI** — the `claude` alias sets `HTTP(S)_PROXY` to the relay:
```sh
claude          # goes through the relay → whatever upstream is selected
```

**Claude desktop app** — GUI apps don't inherit `HTTP(S)_PROXY`, so use the
launcher, which passes Chromium's `--proxy-server`:
```sh
claude-app      # quits + relaunches Claude.app through the relay
```
Launch it this way (not from the Dock), otherwise the flag isn't applied. Tip:
drag `~/.proxy-relay/claude-desktop.command` into the Dock and use that.

**Verify** which exit you're on:
```sh
curl -sS -x http://127.0.0.1:17872 https://api.ipify.org   # exit IP for the active upstream
lsof -nP -iTCP:17872 -sTCP:ESTABLISHED                     # live connections through the relay
```

**Route everything (optional):** instead of per-app config, set a system proxy
in *System Settings → Network → … → Proxies → Web/Secure Web Proxy* to
`127.0.0.1 : 17872`. Then all system-proxy-aware apps use the relay.

## Icon

The icon is a native SF Symbol in template mode, so it auto-adapts to light/dark
menubars. Change `icon.symbol` in `config.json` and hit **Reload config**. To
browse options, render a palette:

```sh
~/.proxy-relay/.venv/bin/python ~/.proxy-relay/make_icon.py           # writes preview.png with candidates
~/.proxy-relay/.venv/bin/python ~/.proxy-relay/make_icon.py network   # set icon.png from a specific symbol
```

## Autostart & manual control

It starts at login automatically (LaunchAgent). Manual control:

```sh
launchctl kickstart -k gui/$(id -u)/com.proxyrelay.menubar   # (re)start now
launchctl bootout   gui/$(id -u)/com.proxyrelay.menubar      # stop until next login
launchctl print     gui/$(id -u)/com.proxyrelay.menubar | grep 'state ='
```

## Uninstall

`./uninstall.sh` reverts everything the installer did: stops and removes the
LaunchAgent, strips the alias block from `~/.zshrc` (keeping a timestamped
backup), and removes `~/.proxy-relay`.

```sh
./uninstall.sh                # asks before deleting the config with your credentials
./uninstall.sh --yes          # no prompts, remove everything
./uninstall.sh --keep-config  # remove everything but keep config.json
```

Homebrew Python, if the installer installed it, is left in place.

## Troubleshooting

- **`pip install rumps` fails building `pyobjc-core`** — you're on the Xcode
  python. Install Homebrew Python (`brew install python@3.12`) and re-run
  `./install.sh`.
- **No menubar icon** — check `~/.proxy-relay/relay.log`; make sure the login
  session is a GUI (Aqua) session.
- **Port already in use** — something else holds `17872`
  (`lsof -nP -iTCP:17872 -sTCP:LISTEN`). Change `listen_port` and re-run install.
- **Desktop Claude ignores the proxy** — it was opened from the Dock. Quit it
  and launch via `claude-app`.
- **TLS error to an HTTPS proxy** — self-signed cert; add `"insecure": true` to
  that proxy entry.
- **Every upstream fails, but the hosts are definitely up** — check for a VPN
  client in TUN mode (Happ/xray, sing-box, and friends). It captures *all*
  outbound TCP, so your proxies are reached from its exit node instead of your
  machine, and the handshakes time out. The giveaway: a connection to a port
  nothing listens on still "succeeds" —
  `python3 -c "import socket; socket.create_connection(('192.0.2.1', 9999), timeout=5)"`
  returns instead of timing out. Turn the tunnel off and check again.

## How it works

`proxy_relay.py` is a threaded `CONNECT`-tunneling proxy on `127.0.0.1:<port>`.
Per new connection it reads the currently-selected upstream and either connects
directly, or opens a tunnel through the upstream proxy (doing TLS-to-proxy +
`Proxy-Authorization` for `https://` upstreams). `menubar.py` is a `rumps`
menubar app that edits the active upstream and installs a native SF Symbol icon
on the status item. State switches take effect on the next connection — no
restart of the relay or the client.

## Development

Tests run automatically on GitHub Actions for every push and pull request — you
don't need to run anything locally. They cover the relay core: upstream URL
parsing, `Proxy-Authorization`, config loading/saving, and real traffic pushed
through the relay (direct, via an HTTP proxy, via a TLS `https://` proxy),
including **switching the upstream mid-flight**, plus regression tests for every
bug listed in [CHANGELOG.md](CHANGELOG.md) — keep-alive routing, pipelined
CONNECT, IPv6 targets, half-close, and that TLS verification stays on by
default. `install.sh` / `uninstall.sh` are exercised end-to-end against a
throwaway `HOME`. The menubar layer isn't unit tested — it's a thin AppKit
wrapper that needs a real GUI session.

CI jobs:

| Job | Runner | What it checks |
|-----|--------|----------------|
| `relay core` | ubuntu, Python 3.11–3.13 | the Python suite (stdlib only, no deps) |
| `ruff` | ubuntu | lint |
| `installer scripts` | ubuntu | `zsh -n` syntax, executable bits, the end-to-end install/uninstall test, and that `config.json` was never committed |
| `macOS` | macos-latest | that `rumps`/`pyobjc` installs, the relay runs headless and proxies a live request |

If you do want to run them locally:

```sh
python3 -m unittest discover -s tests -v   # relay core
zsh tests/test_install.sh                  # installer / uninstaller
ruff check src tests                       # lint
```

The tests are hermetic — throwaway servers on `127.0.0.1`, temp config files and
a sandboxed `HOME`. They never touch `~/.proxy-relay`, your LaunchAgent, your
`~/.zshrc`, or your real proxies.

## License

MIT — see [LICENSE](LICENSE).
