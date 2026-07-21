#!/usr/bin/env python3
"""macOS menubar UI for the proxy relay."""

import os
import subprocess

import AppKit
import rumps

from proxy_relay import STATE, ProxyConfigError, parse_upstream

HERE = os.path.dirname(os.path.abspath(__file__))
ICON = os.path.join(HERE, "icon.png")

# Menu bar icon defaults. Overridable via an "icon" block in config.json:
#   "icon": {"symbol": "globe", "point": 16, "weight": "regular"}
# Rendered as a native SF Symbol on the status item (matches system items) rather
# than a PNG, which rumps would otherwise squash to a tiny 20x20pt bitmap.
DEFAULT_ICON = {"symbol": "globe", "point": 16, "weight": "regular"}
_WEIGHTS = {
    "ultralight": AppKit.NSFontWeightUltraLight,
    "thin": AppKit.NSFontWeightThin,
    "light": AppKit.NSFontWeightLight,
    "regular": AppKit.NSFontWeightRegular,
    "medium": AppKit.NSFontWeightMedium,
    "semibold": AppKit.NSFontWeightSemibold,
    "bold": AppKit.NSFontWeightBold,
}


def _symbol_image(spec):
    img = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(
        spec.get("symbol", "globe"), None
    )
    if img is None:
        return None
    cfg = AppKit.NSImageSymbolConfiguration.configurationWithPointSize_weight_scale_(
        float(spec.get("point", 16)),
        _WEIGHTS.get(spec.get("weight", "regular"), AppKit.NSFontWeightRegular),
        AppKit.NSImageSymbolScaleMedium,
    )
    img = img.imageWithSymbolConfiguration_(cfg)
    img.setTemplate_(True)  # let macOS tint it to match the menubar
    return img


class RelayApp(rumps.App):
    def __init__(self):
        # Seed with the PNG so there's no "ProxyRelay" text flash before the
        # real SF Symbol is installed on the status item post-launch.
        kwargs = {"quit_button": None}
        if os.path.exists(ICON):
            kwargs["icon"] = ICON
            kwargs["template"] = True
        super().__init__("ProxyRelay", **kwargs)
        self._rebuild()
        # The status item only exists once the app is running, so install the
        # real icon from a one-shot timer just after launch.
        self._icon_timer = rumps.Timer(self._install_icon, 0.3)
        self._icon_timer.start()

    # ----- icon ---------------------------------------------------------- #
    def _install_icon(self, sender=None):
        if sender is not None:
            sender.stop()
        spec = dict(DEFAULT_ICON)
        spec.update(STATE.snapshot().get("icon") or {})
        img = _symbol_image(spec)
        try:
            button = self._nsapp.nsstatusitem.button()
            if img is not None and button is not None:
                button.setImage_(img)
        except Exception:
            pass  # keep the PNG fallback

    # ----- menu construction ------------------------------------------- #
    def _rebuild(self):
        config = STATE.snapshot()
        active = config.get("active")
        header = rumps.MenuItem(f"Active: {active or 'none'}")  # no callback = greyed label
        items = [header, rumps.separator]
        for p in config.get("proxies", []):
            label = p["name"]
            item = rumps.MenuItem(label, callback=self._select)
            item.state = 1 if label == active else 0
            items.append(item)
        items += [
            rumps.separator,
            rumps.MenuItem("Add proxy…", callback=self._add),
            rumps.MenuItem("Edit list…", callback=self._edit),
            rumps.MenuItem("Reload config", callback=self._reload),
            rumps.separator,
            rumps.MenuItem("Quit", callback=rumps.quit_application),
        ]
        self.menu.clear()
        self.menu = items

    # ----- callbacks --------------------------------------------------- #
    def _select(self, sender):
        STATE.set_active(sender.title)
        self._rebuild()

    def _add(self, _):
        win = rumps.Window(
            message="One line:  name = proxy_url\n"
                    "Leave url empty for a direct (no-proxy) entry.\n"
                    "e.g.  work = https://user:pass@host:8443",
            title="Add / update proxy",
            default_text="",
            ok="Save",
            cancel="Cancel",
            dimensions=(360, 22),
        )
        res = win.run()
        if not res.clicked:
            return
        text = res.text.strip()
        if "=" not in text:
            rumps.alert("Invalid", "Expected format:  name = url")
            return
        name, _, url = text.partition("=")
        name, url = name.strip(), url.strip()
        if not name:
            rumps.alert("Invalid", "The proxy needs a name.")
            return
        try:
            parse_upstream(url)  # reject a bad URL now, not on the next request
        except ProxyConfigError as e:
            rumps.alert("Invalid proxy URL", str(e))
            return
        STATE.upsert_proxy(name, url)
        self._rebuild()

    def _edit(self, _):
        # subprocess (not os.system) so paths with quotes or spaces can't be
        # reinterpreted by the shell.
        subprocess.run(["open", "-t", STATE.config_path], check=False)

    def _reload(self, _):
        try:
            STATE.load()
        except (OSError, ValueError) as e:
            rumps.alert("Reload failed", str(e))
            return
        self._rebuild()
        self._install_icon()  # pick up a changed icon.symbol too


if __name__ == "__main__":
    from proxy_relay import ensure_config, start_server

    ensure_config()
    STATE.load()
    start_server(STATE)
    RelayApp().run()
