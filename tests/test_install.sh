#!/bin/zsh
# End-to-end test for install.sh / uninstall.sh.
#
# Runs them against a throwaway HOME with launchd and the venv skipped, so it is
# safe on a developer machine and works on Linux CI. Exercises the things that
# would silently break a colleague's setup: port propagation into the aliases
# and the launcher, block rewriting on re-install, refusal on a corrupt config,
# and a clean revert.

set -u
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
FAILURES=0

pass() { print -r -- "  ok    $1"; }
fail() { print -r -- "  FAIL  $1"; FAILURES=$((FAILURES + 1)); }

check() {  # check <description> <condition-as-command...>
  local desc="$1"; shift
  if "$@"; then pass "$desc"; else fail "$desc"; fi
}

contains() { grep -qF -- "$2" "$1"; }
count_of() { grep -cF -- "$2" "$1" | tr -d ' '; }

SANDBOX="$(mktemp -d)"
trap 'rm -rf "$SANDBOX"' EXIT
export HOME="$SANDBOX"
mkdir -p "$HOME"
print -r -- "# pre-existing line" > "$HOME/.zshrc"

APP_DIR="$HOME/.proxy-relay"
RC="$HOME/.zshrc"

print -r -- "== install (fresh, port 20001) =="
"$REPO_DIR/install.sh" --port 20001 --skip-agent --skip-deps >/dev/null 2>&1 \
  || fail "installer exited non-zero"

check "config.json created"            test -f "$APP_DIR/config.json"
# GNU stat first: on Linux `-f` means --file-system and *succeeds* with output
# that isn't a mode, so a BSD-first probe would silently compare garbage.
mode_of() { stat -c '%a' "$1" 2>/dev/null || stat -f '%Lp' "$1"; }
check "config.json is chmod 600"       test "$(mode_of "$APP_DIR/config.json")" = "600"
check "port written to config"         python3 -c "import json,sys; sys.exit(0 if json.load(open('$APP_DIR/config.json'))['listen_port']==20001 else 1)"
check "alias block added"              contains "$RC" "menubar-proxy-switcher"
check "alias uses chosen port"         contains "$RC" "127.0.0.1:20001"
check "launcher uses chosen port"      contains "$APP_DIR/claude-desktop.command" "127.0.0.1:20001"
check "pre-existing rc line kept"      contains "$RC" "# pre-existing line"
check "app files copied"               test -f "$APP_DIR/proxy_relay.py"
check "LaunchAgent plist written"      test -f "$HOME/Library/LaunchAgents/com.proxyrelay.menubar.plist"

print -r -- "== re-install (port 20002) must rewrite, not duplicate =="
"$REPO_DIR/install.sh" --port 20002 --skip-agent --skip-deps >/dev/null 2>&1 \
  || fail "re-install exited non-zero"

check "alias block still appears once" test "$(count_of "$RC" '# >>> menubar-proxy-switcher >>>')" = "1"
check "alias port updated"             contains "$RC" "127.0.0.1:20002"
check "old alias port gone"            eval '! grep -qF "127.0.0.1:20001" "$RC"'
check "launcher port updated"          contains "$APP_DIR/claude-desktop.command" "127.0.0.1:20002"

print -r -- "== re-install with no --port keeps the existing port =="
"$REPO_DIR/install.sh" --skip-agent --skip-deps >/dev/null 2>&1 </dev/null \
  || fail "non-interactive re-install exited non-zero"
check "port preserved when unspecified" contains "$RC" "127.0.0.1:20002"

print -r -- "== corrupt config must abort, not desync =="
cp "$APP_DIR/config.json" "$SANDBOX/config.backup"
print -r -- '{"listen_port": 20002,,}' > "$APP_DIR/config.json"
if "$REPO_DIR/install.sh" --port 20003 --skip-agent --skip-deps >/dev/null 2>&1; then
  fail "installer accepted a corrupt config.json"
else
  pass "installer refused a corrupt config.json"
fi
check "aliases untouched after refusal" eval '! grep -qF "127.0.0.1:20003" "$RC"'
cp "$SANDBOX/config.backup" "$APP_DIR/config.json"

print -r -- "== uninstall reverts everything =="
"$REPO_DIR/uninstall.sh" --yes >/dev/null 2>&1 || fail "uninstaller exited non-zero"

check "app dir removed"                eval '! test -d "$APP_DIR"'
check "plist removed"                  eval '! test -f "$HOME/Library/LaunchAgents/com.proxyrelay.menubar.plist"'
check "alias block removed"            eval '! grep -qF "menubar-proxy-switcher" "$RC"'
check "no stale alias port left"       eval '! grep -qF "127.0.0.1:20002" "$RC"'
check "pre-existing rc line survived"  contains "$RC" "# pre-existing line"
check "rc backup was made"             eval 'ls "$RC".proxyrelay-backup-* >/dev/null 2>&1'

print -r -- ""
if [ "$FAILURES" -eq 0 ]; then
  print -r -- "All installer checks passed."
  exit 0
fi
print -r -- "$FAILURES installer check(s) failed."
exit 1
