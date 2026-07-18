#!/bin/zsh
# Installer for the macOS menubar proxy switcher.
# Lays everything down in ~/.proxy-relay, sets up a LaunchAgent (autostart at
# login), seeds config.json, and adds convenience aliases to ~/.zshrc.
#
#   ./install.sh              # interactive: asks for the relay port
#   ./install.sh --port 12345 # non-interactive
#
# Fails fast: a half-finished install that leaves the alias port and the relay
# port disagreeing is worse than no install at all.
set -e

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$HOME/.proxy-relay"
LABEL="com.proxyrelay.menubar"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DEFAULT_PORT=17872
MARK_START="# >>> menubar-proxy-switcher >>>"
MARK_END="# <<< menubar-proxy-switcher <<<"

# --- 0. Args ---------------------------------------------------------------- #
ARG_PORT=""
SKIP_AGENT=""
SKIP_DEPS=""
while [ $# -gt 0 ]; do
  case "$1" in
    --port) ARG_PORT="$2"; shift 2 ;;
    --port=*) ARG_PORT="${1#*=}"; shift ;;
    # Used by the test suite to exercise this script without touching launchd
    # or building a venv. Not meant for normal installs.
    --skip-agent) SKIP_AGENT=1; shift ;;
    --skip-deps) SKIP_DEPS=1; shift ;;
    -h|--help)
      echo "Usage: ./install.sh [--port N]"
      exit 0 ;;
    *) shift ;;
  esac
done

valid_port() {
  case "$1" in
    ''|*[!0-9]*) return 1 ;;
  esac
  [ "$1" -ge 1024 ] && [ "$1" -le 65535 ]
}

echo "==> Installing menubar proxy switcher into $APP_DIR"

# --- 1. Find a Python that can install pyobjc/rumps ------------------------- #
# The Xcode/CommandLineTools python3 CANNOT build pyobjc — we need Homebrew or
# python.org CPython, which ship prebuilt pyobjc wheels.
PY=""
for c in \
  /opt/homebrew/opt/python@3.13/bin/python3.13 \
  /opt/homebrew/opt/python@3.12/bin/python3.12 \
  /usr/local/opt/python@3.13/bin/python3.13 \
  /usr/local/opt/python@3.12/bin/python3.12 \
  python3.13 python3.12 python3.11; do
  if command -v "$c" >/dev/null 2>&1; then PY="$(command -v "$c")"; break; fi
done

if [ -z "$PY" ] && [ -n "$SKIP_DEPS" ] && command -v python3 >/dev/null 2>&1; then
  # No venv is being built, so any Python can do the JSON/text work.
  PY="$(command -v python3)"
fi

if [ -z "$PY" ]; then
  if command -v brew >/dev/null 2>&1; then
    echo "==> No suitable Python found — installing python@3.12 via Homebrew"
    brew install python@3.12
    PY="$(brew --prefix)/opt/python@3.12/bin/python3.12"
  else
    echo "ERROR: need Homebrew or python.org Python (the Xcode python can't build pyobjc)." >&2
    echo "  Install Homebrew from https://brew.sh, then re-run this script." >&2
    exit 1
  fi
fi
echo "==> Using Python: $PY ($($PY --version 2>&1))"

# --- 2. Copy app files ------------------------------------------------------ #
mkdir -p "$APP_DIR"
cp "$REPO_DIR/src/proxy_relay.py" "$REPO_DIR/src/menubar.py" "$REPO_DIR/src/make_icon.py" "$APP_DIR/"

# --- 3. Seed config.json (keep an existing one) ----------------------------- #
SEEDED=""
if [ ! -f "$APP_DIR/config.json" ]; then
  cp "$REPO_DIR/config.example.json" "$APP_DIR/config.json"
  chmod 600 "$APP_DIR/config.json"
  SEEDED=1
fi

# --- 4. Read the current config (bail loudly if it isn't valid JSON) -------- #
# A malformed config here would otherwise let the script continue and write
# aliases for a port the relay never binds.
if ! CONFIG_INFO="$("$PY" - "$APP_DIR/config.json" <<'PYEOF'
import json, sys
path = sys.argv[1]
try:
    with open(path) as fh:
        cfg = json.load(fh)
except Exception as exc:
    sys.stderr.write(f"ERROR: {path} is not valid JSON: {exc}\n")
    raise SystemExit(2)
print(cfg.get("listen_port", ""))
print((cfg.get("icon") or {}).get("symbol", "shuffle"))
PYEOF
)"; then
  echo "Fix $APP_DIR/config.json and re-run this script." >&2
  exit 1
fi
CURRENT_PORT="${CONFIG_INFO%%$'\n'*}"
SYMBOL="${CONFIG_INFO##*$'\n'}"
valid_port "$CURRENT_PORT" || CURRENT_PORT="$DEFAULT_PORT"

# --- 5. Choose the relay port ----------------------------------------------- #
# Default to the port already in config.json, so re-running never surprises an
# existing install by moving its port.
if [ -n "$ARG_PORT" ]; then
  if valid_port "$ARG_PORT"; then
    PORT="$ARG_PORT"
  else
    echo "ERROR: --port must be a number between 1024 and 65535" >&2
    exit 1
  fi
elif [ -t 0 ]; then
  echo
  echo "The relay listens on 127.0.0.1:<port>. Clients point at this address once;"
  echo "you then switch upstream proxies from the menubar without touching them."
  while true; do
    printf "Relay port [%s]: " "$CURRENT_PORT"
    read -r ans
    if [ -z "$ans" ]; then PORT="$CURRENT_PORT"; break; fi
    if valid_port "$ans"; then PORT="$ans"; break; fi
    echo "  Please enter a number between 1024 and 65535 (or press Enter for $CURRENT_PORT)."
  done
  echo
else
  PORT="$CURRENT_PORT"
fi

if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "!!  Note: something is already listening on port $PORT."
  echo "    If that's a previous version of this relay, it will be replaced below."
fi

# Write the chosen port back atomically — a crash mid-write must never leave a
# truncated config.json, because that file holds the user's proxy credentials.
"$PY" - "$APP_DIR/config.json" "$PORT" <<'PYEOF'
import json, os, sys
path, port = sys.argv[1], int(sys.argv[2])
with open(path) as fh:
    cfg = json.load(fh)
cfg["listen_port"] = port
tmp = path + ".tmp"
with open(tmp, "w") as fh:
    json.dump(cfg, fh, indent=2)
os.chmod(tmp, 0o600)
os.replace(tmp, path)
PYEOF
echo "==> Relay port: $PORT"

# --- 6. venv + dependencies -------------------------------------------------- #
if [ -z "$SKIP_DEPS" ]; then
  if [ ! -x "$APP_DIR/.venv/bin/python" ]; then
    echo "==> Creating virtualenv"
    "$PY" -m venv "$APP_DIR/.venv"
  fi
  echo "==> Installing dependencies (pinned in requirements.txt)"
  "$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
  "$APP_DIR/.venv/bin/pip" install --quiet -r "$REPO_DIR/requirements.txt"

  # --- 7. Seed the menubar icon PNG ----------------------------------------- #
  "$APP_DIR/.venv/bin/python" "$APP_DIR/make_icon.py" "$SYMBOL" >/dev/null 2>&1 || true
fi

# --- 8. LaunchAgent (autostart at login) ------------------------------------ #
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$APP_DIR/.venv/bin/python</string>
        <string>$APP_DIR/menubar.py</string>
    </array>
    <key>WorkingDirectory</key><string>$APP_DIR</string>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><false/>
    <key>StandardOutPath</key><string>$APP_DIR/relay.log</string>
    <key>StandardErrorPath</key><string>$APP_DIR/relay.log</string>
</dict>
</plist>
PLISTEOF

if [ -z "$SKIP_AGENT" ]; then
  echo "==> Loading LaunchAgent"
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || true
fi

# The log sits next to a 600 config; don't let it be world-readable.
touch "$APP_DIR/relay.log"
chmod 600 "$APP_DIR/relay.log" || true

# --- 9. Desktop-app launcher (Claude.app via Chromium --proxy-server) ------- #
cat > "$APP_DIR/claude-desktop.command" <<CMDEOF
#!/bin/zsh
# Launch the Claude desktop app routed through the relay. Must be launched this
# way (not from the Dock) so the --proxy-server flag is applied.
if pgrep -x Claude >/dev/null; then
  osascript -e 'quit app "Claude"' 2>/dev/null
  for i in {1..30}; do pgrep -x Claude >/dev/null || break; sleep 0.3; done
fi
open -a Claude --args --proxy-server="http://127.0.0.1:$PORT"
echo "Launched Claude via relay on port $PORT"
CMDEOF
chmod +x "$APP_DIR/claude-desktop.command"

# --- 10. Shell aliases (guarded, rewritten on re-install) ------------------- #
RC="$HOME/.zshrc"
if grep -qF "$MARK_START" "$RC" 2>/dev/null; then
  # Drop the old block so the port stays in sync on re-install.
  "$PY" - "$RC" "$MARK_START" "$MARK_END" <<'PYEOF'
import sys
path, start, end = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path) as fh:
    lines = fh.readlines()
out, skip = [], False
for line in lines:
    stripped = line.strip()
    if stripped == start:
        skip = True
        continue
    if stripped == end:
        skip = False
        continue
    if not skip:
        out.append(line)
with open(path, "w") as fh:
    fh.writelines(out)
PYEOF
  echo "==> Refreshed alias block in $RC"
else
  echo "==> Added 'claude' and 'claude-app' aliases to $RC"
fi

cat >> "$RC" <<RCEOF

$MARK_START
# Route the Claude CLI through the local relay (switch upstream in the menubar):
alias claude='HTTPS_PROXY="http://127.0.0.1:$PORT" HTTP_PROXY="http://127.0.0.1:$PORT" https_proxy="http://127.0.0.1:$PORT" http_proxy="http://127.0.0.1:$PORT" claude'
# Launch the Claude desktop app through the relay:
alias claude-app='\$HOME/.proxy-relay/claude-desktop.command'
$MARK_END
RCEOF

# --- 11. Verify ------------------------------------------------------------- #
if [ -z "$SKIP_AGENT" ]; then
  sleep 2
  if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "==> Relay is running and listening on 127.0.0.1:$PORT ✓"
  else
    echo "!!  Relay does not appear to be listening yet. Check $APP_DIR/relay.log" >&2
  fi
fi

echo
echo "Done. A menubar icon should now be visible in the top bar."
if [ -n "$SEEDED" ]; then
  echo
  echo "NEXT: add your real proxies —"
  echo "   $APP_DIR/config.json"
  echo "   (or menubar → Edit list…), then menubar → Reload config."
fi
echo "Open a NEW terminal (or run: source $RC) to use 'claude' / 'claude-app'."
