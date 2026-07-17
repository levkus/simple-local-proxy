#!/usr/bin/env python3
"""macOS menubar UI for the proxy relay."""

import os

import AppKit
import rumps

from proxy_relay import STATE, CONFIG_PATH

HERE = os.path.dirname(os.path.abspath(__file__))
ICON = os.path.join(HERE, "icon.png")

# Menu bar icon defaults. Overridable via an "icon" block in config.json:
#   "icon": {"symbol": "shuffle", "point": 16, "weight": "regular"}
# Rendered as a native SF Symbol on the status item (matches system items) rather
# than a PNG, which rumps would otherwise squash to a tiny 20x20pt bitmap.
DEFAULT_ICON = {"symbol": "shuffle", "point": 16, "weight": "regular"}
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
        spec.get("symbol", "shuffle"), None
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
        self._icon_timer = rumps.Timer(self._install_icon, 0.3)
        self._icon_timer.start()

    def _install_icon(self, sender):
        sender.stop()
        spec = dict(DEFAULT_ICON)
        spec.update(STATE.config.get("icon") or {})
        img = _symbol_image(spec)
        try:
            button = self._nsapp.nsstatusitem.button()
            if img is not None and button is not None:
                button.setImage_(img)
        except Exception:
            pass  # keep the PNG fallback

    # ----- menu construction ------------------------------------------- #
    def _rebuild(self):
        active = STATE.config.get("active")
        header = rumps.MenuItem(f"Active: {active or 'none'}")  # no callback = greyed label
        items = [header, rumps.separator]
        for p in STATE.config.get("proxies", []):
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
            return
        with STATE.lock:
            STATE.config["proxies"] = [
                p for p in STATE.config["proxies"] if p.get("name") != name
            ]
            STATE.config["proxies"].append({"name": name, "url": url})
            STATE._resolve()
            STATE.save_locked()
        self._rebuild()

    def _edit(self, _):
        os.system(f"open -t {CONFIG_PATH!r}")

    def _reload(self, _):
        try:
            STATE.load()
        except Exception as e:
            rumps.alert("Reload failed", str(e))
            return
        self._rebuild()


if __name__ == "__main__":
    from proxy_relay import ensure_config, start_server

    ensure_config()
    STATE.load()
    start_server()
    RelayApp().run()
