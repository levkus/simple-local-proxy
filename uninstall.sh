#!/bin/zsh
# Stop and remove the menubar proxy switcher.
LABEL="com.proxyrelay.menubar"
APP_DIR="$HOME/.proxy-relay"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
echo "Stopped and removed the LaunchAgent."

echo "App files remain in $APP_DIR."
echo "  Delete them with:   rm -rf \"$APP_DIR\""
echo "Remove the alias block between the 'menubar-proxy-switcher' markers in ~/.zshrc if you added it."
