# Menubar Proxy Switcher (macOS)

A tiny local proxy relay with a menubar icon that lets you **switch between
upstream proxies with a click** — no restarting the app that's using the proxy.

```
  your app  ──►  relay (127.0.0.1:13546)  ──►  upstream selected in the menubar
 (CLI / GUI)      always the same address        work / backup / direct / …
```

You point a client at the relay **once** (a fixed local address). The relay
tunnels each new connection through whichever upstream you've picked in the
menubar. Flip the upstream in the menu and the client keeps talking to the same
`127.0.0.1:13546` — only *new* connections use the new upstream. Nothing to
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

The installer:
- copies the app into `~/.proxy-relay/` and builds a venv with `rumps`,
- seeds `~/.proxy-relay/config.json` from `config.example.json`,
- installs a **LaunchAgent** so it starts automatically at every login,
- generates the menubar icon,
- adds `claude` and `claude-app` aliases to `~/.zshrc`.

Then put your real proxies in `~/.proxy-relay/config.json` (or menubar →
**Edit list…**) and hit menubar → **Reload config**.

> Credentials live only in `config.json`, which is git-ignored. Never commit it.

## Configure

`config.json`:

```json
{
  "listen_host": "127.0.0.1",
  "listen_port": 13546,
  "active": "work-https",
  "icon": { "symbol": "shuffle", "point": 16, "weight": "regular" },
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

`listen_port` is configurable; the aliases and the desktop launcher are
generated from it at install time. If you change the port later, re-run
`./install.sh` (it's idempotent) so they stay in sync.

## Use it

**Menubar** — click the icon:
- pick a proxy (checkmark = active; `Active: …` shows the current one),
- **Add proxy…** — quick `name = url` entry,
- **Edit list…** — open `config.json`,
- **Reload config** — re-read the file (also re-applies the icon).

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
curl -sS -x http://127.0.0.1:13546 https://api.ipify.org   # exit IP for the active upstream
lsof -nP -iTCP:13546 -sTCP:ESTABLISHED                     # live connections through the relay
```

**Route everything (optional):** instead of per-app config, set a system proxy
in *System Settings → Network → … → Proxies → Web/Secure Web Proxy* to
`127.0.0.1 : 13546`. Then all system-proxy-aware apps use the relay.

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

```sh
./uninstall.sh
# then optionally: rm -rf ~/.proxy-relay
# and remove the alias block in ~/.zshrc between the "menubar-proxy-switcher" markers
```

## Troubleshooting

- **`pip install rumps` fails building `pyobjc-core`** — you're on the Xcode
  python. Install Homebrew Python (`brew install python@3.12`) and re-run
  `./install.sh`.
- **No menubar icon** — check `~/.proxy-relay/relay.log`; make sure the login
  session is a GUI (Aqua) session.
- **Port already in use** — something else holds `13546`
  (`lsof -nP -iTCP:13546 -sTCP:LISTEN`). Change `listen_port` and re-run install.
- **Desktop Claude ignores the proxy** — it was opened from the Dock. Quit it
  and launch via `claude-app`.
- **TLS error to an HTTPS proxy** — self-signed cert; add `"insecure": true` to
  that proxy entry.

## How it works

`proxy_relay.py` is a threaded `CONNECT`-tunneling proxy on `127.0.0.1:<port>`.
Per new connection it reads the currently-selected upstream and either connects
directly, or opens a tunnel through the upstream proxy (doing TLS-to-proxy +
`Proxy-Authorization` for `https://` upstreams). `menubar.py` is a `rumps`
menubar app that edits the active upstream and installs a native SF Symbol icon
on the status item. State switches take effect on the next connection — no
restart of the relay or the client.

## License

MIT — see [LICENSE](LICENSE).
