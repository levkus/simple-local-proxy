#!/bin/zsh
# Uninstaller — reverts everything install.sh did.
#
#   ./uninstall.sh                # interactive (asks before deleting your config)
#   ./uninstall.sh --yes          # no prompts, remove everything
#   ./uninstall.sh --keep-config  # remove everything except ~/.proxy-relay/config.json

APP_DIR="$HOME/.proxy-relay"
LABEL="com.proxyrelay.menubar"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
RC="$HOME/.zshrc"
MARK_START="# >>> menubar-proxy-switcher >>>"
MARK_END="# <<< menubar-proxy-switcher <<<"

ASSUME_YES=""
KEEP_CONFIG=""
while [ $# -gt 0 ]; do
  case "$1" in
    --yes|-y) ASSUME_YES=1; shift ;;
    --keep-config) KEEP_CONFIG=1; shift ;;
    -h|--help)
      echo "Usage: ./uninstall.sh [--yes] [--keep-config]"
      exit 0 ;;
    *) shift ;;
  esac
done

echo "==> Uninstalling menubar proxy switcher"

# --- 1. Stop and remove the LaunchAgent ------------------------------------- #
if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null
  echo "    stopped LaunchAgent ($LABEL)"
else
  echo "    LaunchAgent not loaded"
fi
if [ -f "$PLIST" ]; then
  rm -f "$PLIST"
  echo "    removed $PLIST"
fi

# Belt and braces: kill any stray menubar process still holding the port.
pkill -f "$APP_DIR/menubar.py" 2>/dev/null && echo "    killed stray relay process"

# --- 2. Strip the alias block from ~/.zshrc --------------------------------- #
if [ -f "$RC" ] && grep -qF "$MARK_START" "$RC"; then
  BACKUP="$RC.proxyrelay-backup-$(date +%Y%m%d%H%M%S)"
  cp "$RC" "$BACKUP"
  TMP="$(mktemp)"
  awk -v s="$MARK_START" -v e="$MARK_END" '
    index($0, s) { skip = 1; next }
    index($0, e) { skip = 0; next }
    !skip { print }
  ' "$RC" > "$TMP" && mv "$TMP" "$RC"
  echo "    removed alias block from $RC (backup: $BACKUP)"
else
  echo "    no alias block in $RC"
fi

# --- 3. Remove the app directory -------------------------------------------- #
if [ -d "$APP_DIR" ]; then
  if [ -n "$KEEP_CONFIG" ]; then
    find "$APP_DIR" -mindepth 1 -maxdepth 1 ! -name 'config.json' -exec rm -rf {} +
    echo "    removed $APP_DIR contents, kept config.json"
  else
    REPLY_OK="$ASSUME_YES"
    if [ -z "$REPLY_OK" ] && [ -t 0 ]; then
      echo
      echo "    $APP_DIR contains config.json with your proxy credentials."
      printf "    Delete the whole directory? [y/N]: "
      read -r ans
      case "$ans" in [yY]*) REPLY_OK=1 ;; esac
    fi
    if [ -n "$REPLY_OK" ]; then
      rm -rf "$APP_DIR"
      echo "    removed $APP_DIR"
    else
      echo "    kept $APP_DIR (delete manually: rm -rf \"$APP_DIR\")"
    fi
  fi
else
  echo "    $APP_DIR not present"
fi

echo
echo "Done — the menubar icon is gone and the relay is stopped."
echo "Notes:"
echo "  • Open a new terminal so the removed 'claude' / 'claude-app' aliases stop applying."
echo "  • If Claude.app is still running with --proxy-server, quit and reopen it normally."
echo "  • Homebrew Python installed by install.sh (if any) was left alone."
