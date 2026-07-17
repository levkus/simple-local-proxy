#!/bin/zsh
# Installer for the macOS menubar proxy switcher.
# Lays everything down in ~/.proxy-relay, sets up a LaunchAgent (autostart at
# login), seeds config.json, and adds convenience aliases to ~/.zshrc.

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$HOME/.proxy-relay"
LABEL="com.proxyrelay.menubar"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

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

# --- 4. venv + rumps -------------------------------------------------------- #
if [ ! -x "$APP_DIR/.venv/bin/python" ]; then
  echo "==> Creating virtualenv"
  "$PY" -m venv "$APP_DIR/.venv" || { echo "venv creation failed" >&2; exit 1; }
fi
echo "==> Installing rumps (menubar library)"
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet rumps || { echo "pip install rumps failed" >&2; exit 1; }

VPY="$APP_DIR/.venv/bin/python"
PORT="$("$VPY" -c "import json;print(json.load(open('$APP_DIR/config.json')).get('listen_port',13546))")"
SYMBOL="$("$VPY" -c "import json;print((json.load(open('$APP_DIR/config.json')).get('icon') or {}).get('symbol','shuffle'))")"

# --- 5. Seed the menubar icon PNG ------------------------------------------- #
"$VPY" "$APP_DIR/make_icon.py" "$SYMBOL" >/dev/null 2>&1 || true

# --- 6. LaunchAgent (autostart at login) ------------------------------------ #
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

# --- 7. Desktop-app launcher (Claude.app via Chromium --proxy-server) ------- #
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

# --- 8. Shell aliases (guarded) --------------------------------------------- #
RC="$HOME/.zshrc"
MARK_START="# >>> menubar-proxy-switcher >>>"
if ! grep -qF "$MARK_START" "$RC" 2>/dev/null; then
  cat >> "$RC" <<RCEOF

$MARK_START
# Route the Claude CLI through the local relay (switch upstream in the menubar):
alias claude='HTTPS_PROXY="http://127.0.0.1:$PORT" HTTP_PROXY="http://127.0.0.1:$PORT" https_proxy="http://127.0.0.1:$PORT" http_proxy="http://127.0.0.1:$PORT" claude'
# Launch the Claude desktop app through the relay:
alias claude-app='\$HOME/.proxy-relay/claude-desktop.command'
# <<< menubar-proxy-switcher <<<
RCEOF
  echo "==> Added 'claude' and 'claude-app' aliases to $RC"
else
  echo "==> Aliases already present in $RC (skipped)"
fi

# --- 9. Verify -------------------------------------------------------------- #
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
