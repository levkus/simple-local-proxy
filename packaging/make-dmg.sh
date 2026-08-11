#!/bin/sh
# Package dist/ProxyRelay.app into a DMG for handing to other people.
#
#   ./packaging/build-app.sh        # build the bundle first
#   ./packaging/make-dmg.sh         # -> dist/ProxyRelay-<arch>.dmg
#
# Signing, in increasing order of how smoothly it lands on someone else's Mac:
#
#   (nothing)                  ad-hoc. Gatekeeper blocks it after download; the
#                              recipient has to clear the quarantine flag by
#                              hand. Fine for a handful of colleagues you can
#                              talk to, painful for strangers.
#   CODESIGN_IDENTITY=...      a Developer ID Application certificate. Signed,
#                              but still quarantined until notarised.
#   + NOTARY_PROFILE=...       notarised and stapled: opens with a double click,
#                              no warnings. Needs a paid Apple Developer account
#                              and a stored notarytool profile:
#                              xcrun notarytool store-credentials <profile> \
#                                  --apple-id ... --team-id ... --password ...
#
# The bundle is built for the machine that builds it — an arm64 build will not
# run on an Intel Mac. Build on each architecture you need to support.

set -eu

cd "$(dirname "$0")/.."
APP="dist/ProxyRelay.app"
ARCH="$(uname -m)"
DMG="dist/ProxyRelay-$ARCH.dmg"
STAGE="build/dmg"

[ -d "$APP" ] || {
    echo "$APP not found — run ./packaging/build-app.sh first" >&2
    exit 1
}

if [ -n "${CODESIGN_IDENTITY:-}" ]; then
    echo "==> Re-signing the bundle with $CODESIGN_IDENTITY"
    # A hardened runtime is required for notarisation.
    codesign --force --deep --options runtime --timestamp \
        --sign "$CODESIGN_IDENTITY" "$APP"
fi

echo "==> Staging"
rm -rf "$STAGE" "$DMG"
mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"   # drag-to-install target
cp LICENSE "$STAGE/LICENSE.txt"

# Recipients of an unsigned build hit Gatekeeper, and the error macOS shows
# ("damaged and can't be opened") suggests a corrupt download rather than a
# missing signature. Say so up front, in the disk image itself.
if [ -z "${CODESIGN_IDENTITY:-}" ]; then
    cat > "$STAGE/READ ME FIRST.txt" <<'TXT'
ProxyRelay is ad-hoc signed, not notarised by Apple.

macOS will refuse to open it after download, and the message it shows
("ProxyRelay is damaged and can't be opened") is misleading — nothing is
damaged, the app simply isn't signed with a paid Apple developer certificate.

To install:

  1. Drag ProxyRelay.app onto the Applications folder in this window.
  2. Open Terminal and run:

       xattr -dr com.apple.quarantine /Applications/ProxyRelay.app

  3. Launch it from Applications. The icon appears in the menubar (there is
     no Dock icon by design).

Only do this for software you got from someone you trust. That command clears
the check macOS does on downloaded software, so it is worth understanding
before running it.

Configuration lives in ~/.proxy-relay/config.json.
TXT
fi

echo "==> Building $DMG"
hdiutil create -quiet -volname "ProxyRelay" -srcfolder "$STAGE" \
    -ov -format UDZO "$DMG"

if [ -n "${CODESIGN_IDENTITY:-}" ]; then
    echo "==> Signing the disk image"
    codesign --force --sign "$CODESIGN_IDENTITY" "$DMG"
fi

if [ -n "${NOTARY_PROFILE:-}" ]; then
    echo "==> Notarising (this takes a few minutes)"
    xcrun notarytool submit "$DMG" --keychain-profile "$NOTARY_PROFILE" --wait
    xcrun stapler staple "$DMG"
    echo "    stapled — opens with a double click on any Mac"
fi

echo "==> Done: $DMG ($(du -h "$DMG" | cut -f1)), $ARCH only"
[ -n "${CODESIGN_IDENTITY:-}" ] || echo "    unsigned — recipients must clear the quarantine flag (see READ ME FIRST.txt)"
