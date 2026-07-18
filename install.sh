#!/bin/zsh
# Installer for the macOS menubar proxy switcher.
# Lays everything down in ~/.proxy-relay, sets up a LaunchAgent (autostart at
# login), seeds config.json, and adds convenience aliases to ~/.zshrc.
#
#   ./install.sh              # interactive: asks for the relay port
#   ./install.sh --port 12345 # non-interactive

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$HOME/.proxy-relay"
LABEL="com.proxyrelay.menubar"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DEFAULT_PORT=17872

# --- 0. Args ---------------------------------------------------------------- #
ARG_PORT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --port) ARG_PORT="$2"; shift 2 ;;
    --port=*) ARG_PORT="${1#*=}"; shift ;;
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

if [ -z "$PY" ]; then
  if command -v brew >/dev/null 2>&1; then
    echo "==> No suitable Python found — installing python@3.12 via Homebrew"
    brew install python@3.12 || { echo "brew install failed" >&2; exit 1; }
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

# --- 4. Choose the relay port ----------------------------------------------- #
# Default to the port already in config.json (so re-running never surprises an
# existing install), otherwise to DEFAULT_PORT.
CURRENT_PORT="$("$PY" -c "import json;print(json.load(open('$APP_DIR/config.json')).get('listen_port', $DEFAULT_PORT))" 2>/dev/null)"
valid_port "$CURRENT_PORT" || CURRENT_PORT="$DEFAULT_PORT"

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

# Warn if the port is taken by something that isn't our own agent.
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "!!  Note: something is already listening on port $PORT."
  echo "    If that's a previous version of this relay, it will be replaced below."
fi

"$PY" - "$APP_DIR/config.json" "$PORT" <<'PYEOF'
import json, sys
path, port = sys.argv[1], int(sys.argv[2])
cfg = json.load(open(path))
cfg["listen_port"] = port
json.dump(cfg, open(path, "w"), indent=2)
PYEOF
echo "==> Relay port: $PORT"

# --- 5. venv + rumps -------------------------------------------------------- #
if [ ! -x "$APP_DIR/.venv/bin/python" ]; then
  echo "==> Creating virtualenv"
  "$PY" -m venv "$APP_DIR/.venv" || { echo "venv creation failed" >&2; exit 1; }
fi
echo "==> Installing rumps (menubar library)"
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet rumps || { echo "pip install rumps failed" >&2; exit 1; }

VPY="$APP_DIR/.venv/bin/python"
SYMBOL="$("$VPY" -c "import json;print((json.load(open('$APP_DIR/config.json')).get('icon') or {}).get('symbol','shuffle'))")"

# --- 6. Seed the menubar icon PNG ------------------------------------------- #
"$VPY" "$APP_DIR/make_icon.py" "$SYMBOL" >/dev/null 2>&1 || true

# --- 7. LaunchAgent (autostart at login) ------------------------------------ #
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

echo "==> Loading LaunchAgent"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null
launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null

# --- 8. Desktop-app launcher (Claude.app via Chromium --proxy-server) ------- #
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

# --- 9. Shell aliases (guarded, rewritten on re-install) -------------------- #
RC="$HOME/.zshrc"
MARK_START="# >>> menubar-proxy-switcher >>>"
MARK_END="# <<< menubar-proxy-switcher <<<"
if grep -qF "$MARK_START" "$RC" 2>/dev/null; then
  # Drop the old block so the port stays in sync on re-install.
  "$PY" - "$RC" "$MARK_START" "$MARK_END" <<'PYEOF'
import sys
path, start, end = sys.argv[1], sys.argv[2], sys.argv[3]
lines = open(path).read().splitlines(True)
out, skip = [], False
for ln in lines:
    if ln.strip() == start:
        skip = True
        continue
    if ln.strip() == end:
        skip = False
        continue
    if not skip:
        out.append(ln)
open(path, "w").writelines(out)
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

# --- 10. Verify ------------------------------------------------------------- #
sleep 2
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "==> Relay is running and listening on 127.0.0.1:$PORT ✓"
else
  echo "!!  Relay does not appear to be listening yet. Check $APP_DIR/relay.log" >&2
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
