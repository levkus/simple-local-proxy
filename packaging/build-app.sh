#!/bin/sh
# Build ProxyRelay.app — a self-contained bundle with its own Python.
#
# Why: the LaunchAgent runs .venv/bin/python, so the relay dies with whatever
# Python it was built against. On a managed Mac that Python can vanish (a
# cleanup wiping /opt/homebrew takes brew's python with it) and the relay then
# fails to start — silently, at the next login. A bundle carries its own.
#
#   ./packaging/build-app.sh            # build + ad-hoc sign into dist/
#   ./packaging/build-app.sh --install   # also install and switch the LaunchAgent
#
# Requires uv (https://docs.astral.sh/uv/). It supplies the build Python and a
# throwaway environment, so nothing is added to your runtime venv.

set -eu

cd "$(dirname "$0")/.."
APP="dist/ProxyRelay.app"
DEST="$HOME/Applications/ProxyRelay.app"
PLIST="$HOME/Library/LaunchAgents/com.proxyrelay.menubar.plist"

command -v uv >/dev/null 2>&1 || {
    echo "uv not found — install it from https://docs.astral.sh/uv/ first" >&2
    exit 1
}

echo "==> Building $APP"
uv run --no-project --python 3.12 --with pyinstaller --with rumps \
    pyinstaller --noconfirm --clean --distpath dist --workpath build/pyinstaller \
    packaging/ProxyRelay.spec

# Ad-hoc signature. Not notarised — that needs a paid Developer ID — but it
# keeps Gatekeeper from refusing a locally built bundle outright.
echo "==> Signing (ad-hoc)"
codesign --force --deep --sign - "$APP"
codesign --verify --deep --strict "$APP" && echo "    signature ok"

[ "${1:-}" = "--install" ] || {
    echo "==> Done: $APP"
    echo "    install it with: $0 --install"
    exit 0
}

echo "==> Installing to $DEST"
mkdir -p "$HOME/Applications"
rm -rf "$DEST"
cp -R "$APP" "$DEST"

# Point the existing LaunchAgent at the bundle so login autostart survives.
if [ -f "$PLIST" ]; then
    echo "==> Repointing the LaunchAgent at the bundle"
    /usr/libexec/PlistBuddy -c "Delete :ProgramArguments" "$PLIST" 2>/dev/null || true
    /usr/libexec/PlistBuddy -c "Add :ProgramArguments array" "$PLIST"
    /usr/libexec/PlistBuddy -c "Add :ProgramArguments:0 string $DEST/Contents/MacOS/ProxyRelay" "$PLIST"
    # bootout + bootstrap, not kickstart: kickstart restarts the job from the
    # copy launchd already holds, so an edited plist would be ignored.
    launchctl bootout "gui/$(id -u)/com.proxyrelay.menubar" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
    echo "    restarted"
else
    echo "    no LaunchAgent found — open $DEST yourself, or add it to Login Items"
fi

echo "==> Installed. The bundle no longer depends on ~/.proxy-relay/.venv;"
echo "    config.json and the icon are still read from ~/.proxy-relay."
