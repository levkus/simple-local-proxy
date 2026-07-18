#!/usr/bin/env python3
"""Render SF Symbols to monochrome template PNGs for the menubar icon,
plus a preview contact-sheet so you can pick one.

Usage:
  python make_icon.py            # render all candidates + preview.png
  python make_icon.py <symbol>   # set icon.png from a specific SF Symbol name
"""
import os
import sys

import AppKit
from Foundation import NSMakePoint, NSMakeRect

HERE = os.path.dirname(os.path.abspath(__file__))

# Curated candidates that read as "switch / route / network".
CANDIDATES = [
    # switching / toggling
    "shuffle",
    "switch.2",
    "arrow.left.arrow.right",
    "arrow.triangle.swap",
    "arrow.trianglehead.swap",
    "arrow.triangle.2.circlepath",
    "arrow.triangle.branch",
    # network / routing / nodes
    "network",
    "point.3.connected.trianglepath.dotted",
    "point.3.filled.connected.trianglepath.dotted",
    "globe",
    "antenna.radiowaves.left.and.right",
    "cable.connector",
    "server.rack",
    # secure / vpn-ish
    "network.badge.shield.half.filled",
    "lock.shield",
]


def _symbol(name, point, color):
    img = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, None)
    if img is None:
        return None
    cfg = AppKit.NSImageSymbolConfiguration.configurationWithPointSize_weight_scale_(
        point, AppKit.NSFontWeightRegular, AppKit.NSImageSymbolScaleMedium
    )
    if color is not None:
        pal = AppKit.NSImageSymbolConfiguration.configurationWithPaletteColors_([color])
        cfg = cfg.configurationByApplyingConfiguration_(pal)
    return img.imageWithSymbolConfiguration_(cfg)


def render_template_png(name, path, px=44, point=18):
    """Black glyph on transparent — the actual menubar template icon."""
    glyph = _symbol(name, point, AppKit.NSColor.blackColor())
    if glyph is None:
        return False
    canvas = AppKit.NSImage.alloc().initWithSize_((px, px))
    canvas.lockFocus()
    s = glyph.size()
    glyph.drawInRect_(NSMakeRect((px - s.width) / 2, (px - s.height) / 2, s.width, s.height))
    canvas.unlockFocus()
    rep = AppKit.NSBitmapImageRep.imageRepWithData_(canvas.TIFFRepresentation())
    data = rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, None)
    return bool(data.writeToFile_atomically_(path, True))


def render_preview(path, names):
    """A dark menubar-like strip with each candidate in white + its number/name."""
    row_h, cell, pad = 46, 40, 16
    text_w = 320
    W = pad + cell + 12 + text_w + pad
    H = row_h * len(names) + pad
    canvas = AppKit.NSImage.alloc().initWithSize_((W, H))
    canvas.lockFocus()
    AppKit.NSColor.colorWithCalibratedWhite_alpha_(0.11, 1.0).setFill()
    AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
        NSMakeRect(0, 0, W, H), 14, 14
    ).fill()

    white = AppKit.NSColor.whiteColor()
    attrs = {
        AppKit.NSForegroundColorAttributeName: white,
        AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_(16),
    }
    for i, name in enumerate(names):
        # rows top-to-bottom (flip y since origin is bottom-left)
        y = H - pad / 2 - (i + 1) * row_h
        glyph = _symbol(name, 19, white)
        if glyph is not None:
            s = glyph.size()
            glyph.drawInRect_(
                NSMakeRect(pad + (cell - s.width) / 2, y + (row_h - s.height) / 2, s.width, s.height)
            )
        label = AppKit.NSString.stringWithString_(f"{i + 1}.  {name}")
        label.drawAtPoint_withAttributes_(
            NSMakePoint(pad + cell + 12, y + (row_h - 22) / 2), attrs
        )
    canvas.unlockFocus()
    rep = AppKit.NSBitmapImageRep.imageRepWithData_(canvas.TIFFRepresentation())
    data = rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, None)
    data.writeToFile_atomically_(path, True)


def main():
    if len(sys.argv) > 1:
        name = sys.argv[1]
        ok = render_template_png(name, os.path.join(HERE, "icon.png"))
        print(f"icon.png <- {name}: {'ok' if ok else 'FAILED (unknown symbol)'}")
        return

    ok = [n for n in CANDIDATES if render_template_png(n, os.path.join(HERE, f"icon_{n}.png"))]
    print("rendered:", ", ".join(ok))
    render_preview(os.path.join(HERE, "preview.png"), ok)
    # default pick
    render_template_png(ok[0], os.path.join(HERE, "icon.png"))
    print(f"default icon.png <- {ok[0]}")
    print("preview.png written")


if __name__ == "__main__":
    main()
