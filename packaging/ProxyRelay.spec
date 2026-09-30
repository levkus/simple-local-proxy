# PyInstaller spec for the menubar relay.
#
# Produces a self-contained ProxyRelay.app with its own Python inside, so the
# relay keeps working when the Python it was installed against disappears —
# a managed Mac can lose Homebrew (and everything installed through it) without
# warning, which otherwise takes the relay down with it.
#
# Build with packaging/build-app.sh, not by calling pyinstaller directly.

a = Analysis(
    ["../src/menubar.py"],
    pathex=["../src"],
    hiddenimports=["health", "proxy_relay"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="ProxyRelay",
    console=False,
    argv_emulation=False,
)

coll = COLLECT(exe, a.binaries, a.datas, name="ProxyRelay")

app = BUNDLE(
    coll,
    name="ProxyRelay.app",
    bundle_identifier="com.proxyrelay.menubar",
    info_plist={
        # Menubar-only: no Dock tile, no app switcher entry.
        "LSUIElement": True,
        "CFBundleName": "ProxyRelay",
        "CFBundleDisplayName": "ProxyRelay",
        "NSHighResolutionCapable": True,
        # Nothing here talks to the user's data; declared so the intent is on
        # record if macOS ever asks.
        "NSHumanReadableCopyright": "MIT",
    },
)
