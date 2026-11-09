#!/usr/bin/env python3
"""Fabrique les images de la marque Vlocal à partir de tools/brand_paths.py.

    python tools/make_icon.py            icône de l'app : build/AppIcon.icns
                                         (étape « icône » de build_app.sh, lue
                                         par Vlocal.spec au même chemin)
    python tools/make_icon.py --assets   régénère les fichiers livrés dans assets/ :
                                         le mot, le symbole (SVG en currentColor) et
                                         les gabarits de la barre des menus (PNG)

Les gabarits PNG sont livrés plutôt que rastérisés au lancement : dessiner une
image dans le processus de l'app exige le fil principal Cocoa, que pywebview
possède déjà (voir menubar.py). Le rendu passe par AppKit hors écran, sans
fenêtre, donc sans risque hors du fil principal.
"""
import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import brand_paths  # noqa: E402  (module voisin, pas un paquet)

ASSETS = ROOT / "assets"
WORDMARK_SVG = ASSETS / "vlocal-wordmark.svg"
SYMBOL_SVG = ASSETS / "vlocal-symbol.svg"
MENUBAR_PT = 18
# Gabarits @1x et @2x d'une icône de 18 pt (menubar.py les assemble).
MENUBAR_PNGS = {px: ASSETS / ("vlocal-menubar-%d.png" % px)
                for px in (MENUBAR_PT, MENUBAR_PT * 2)}
# Chemin UNIQUE de l'icône : écrit ici, lu par Vlocal.spec (icon=...).
ICNS_PATH = ROOT / "build" / "AppIcon.icns"

# Bleu nuit du logo, le même que --vl-night dans assets/brand.css.
NIGHT = (0x11 / 255, 0x14 / 255, 0x1F / 255)

# Grille des icônes macOS (1024 px) : carré arrondi de 824 px centré, rayon de
# 185 px, le reste du canevas sert à l'ombre portée.
ICON_CANVAS = 1024
ICON_TILE = 824
ICON_RADIUS = 185.4
ICON_LIFT = 10           # le carré remonte un peu : l'ombre tombe dessous
ICON_SYMBOL_H = 0.44     # hauteur de la bulle rapportée au carré (choix optique)
MENUBAR_SYMBOL_H = 14.0  # hauteur de la bulle dans les 18 pt de la barre des menus


def write_svgs():
    WORDMARK_SVG.write_text(brand_paths.svg_document(brand_paths.wordmark(), "Vlocal"),
                            encoding="utf-8")
    SYMBOL_SVG.write_text(brand_paths.svg_document(brand_paths.symbol(), "Vlocal"),
                          encoding="utf-8")


def _bezier_path(subpaths, scale, ox, oy, canvas_h):
    """NSBezierPath (pair-impair) des sous-tracés, mis à l'échelle et posés en
    (ox, oy) depuis le coin haut-gauche ; AppKit compte y vers le haut."""
    from AppKit import NSBezierPath, NSEvenOddWindingRule
    from Foundation import NSMakePoint

    x0, y0 = brand_paths.bounds(subpaths)[:2]

    def pt(p):
        return NSMakePoint(ox + (p[0] - x0) * scale, canvas_h - (oy + (p[1] - y0) * scale))
    path = NSBezierPath.bezierPath()
    path.setWindingRule_(NSEvenOddWindingRule)
    for sp in subpaths:
        for seg in sp:
            if seg[0] == "M":
                path.moveToPoint_(pt(seg[1]))
            elif seg[0] == "L":
                path.lineToPoint_(pt(seg[1]))
            else:
                path.curveToPoint_controlPoint1_controlPoint2_(pt(seg[3]), pt(seg[1]), pt(seg[2]))
        path.closePath()
    return path


def _render(px, draw, out):
    """Dessine dans une image RGBA px x px hors écran et l'écrit en PNG."""
    from AppKit import (NSBitmapImageRep, NSDeviceRGBColorSpace, NSGraphicsContext,
                        NSBitmapImageFileTypePNG)
    rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, px, px, 8, 4, True, False, NSDeviceRGBColorSpace, 0, 0)
    ctx = NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    NSGraphicsContext.saveGraphicsState()
    try:
        NSGraphicsContext.setCurrentContext_(ctx)
        draw()
        ctx.flushGraphics()
    finally:
        NSGraphicsContext.restoreGraphicsState()
    data = rep.representationUsingType_properties_(NSBitmapImageFileTypePNG, None)
    if data is None or not data.writeToFile_atomically_(str(out), True):
        raise OSError("écriture impossible : %s" % out)


def _symbol_layout(size, symbol_h):
    """Échelle et origine qui centrent la bulle dans un carré de côté size."""
    x0, y0, x1, y1 = brand_paths.bounds(brand_paths.symbol())
    scale = symbol_h / (y1 - y0)
    return scale, (size - (x1 - x0) * scale) / 2.0, (size - (y1 - y0) * scale) / 2.0


def render_menubar_png(px, out):
    """Gabarit de la barre des menus : bulle noire sur fond transparent (macOS
    la recolore selon le thème, image « template »)."""
    from AppKit import NSColor
    scale, ox, oy = _symbol_layout(px, MENUBAR_SYMBOL_H * px / MENUBAR_PT)

    def draw():
        NSColor.blackColor().set()
        _bezier_path(brand_paths.symbol(), scale, ox, oy, px).fill()
    _render(px, draw, out)


def render_icon_png(out):
    """Icône 1024 : bulle blanche sur carré arrondi bleu nuit, ombre portée douce."""
    from AppKit import NSBezierPath, NSColor, NSGradient, NSShadow
    from Foundation import NSMakeRect, NSMakeSize
    size, tile = ICON_CANVAS, ICON_TILE
    inset = (size - tile) / 2.0

    def draw():
        rect = NSMakeRect(inset, inset + ICON_LIFT, tile, tile)
        shape = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            rect, ICON_RADIUS, ICON_RADIUS)
        shadow = NSShadow.alloc().init()
        shadow.setShadowOffset_(NSMakeSize(0, -12))
        shadow.setShadowBlurRadius_(28)
        shadow.setShadowColor_(NSColor.colorWithCalibratedWhite_alpha_(0.0, 0.32))
        shadow.set()
        NSColor.colorWithDeviceRed_green_blue_alpha_(*NIGHT, 1.0).set()
        shape.fill()
        NSShadow.alloc().init().set()                      # pas d'ombre sur le reste
        # Lumière zénithale à peine perceptible : du relief sans changer la teinte.
        light = NSGradient.alloc().initWithStartingColor_endingColor_(
            NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.06),
            NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.0))
        light.drawInBezierPath_angle_(shape, -90)
        scale, ox, oy = _symbol_layout(tile, tile * ICON_SYMBOL_H)
        NSColor.whiteColor().set()
        _bezier_path(brand_paths.symbol(), scale, inset + ox,
                     inset - ICON_LIFT + oy, size).fill()
    _render(size, draw, out)


def build_icns(out=ICNS_PATH):
    """Icône 1024 -> .icns (toutes les tailles du jeu d'icônes, via sips et iconutil)."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="vlocal_icon_") as tmp:
        master = Path(tmp) / "icon_1024.png"
        render_icon_png(master)
        iconset = Path(tmp) / "AppIcon.iconset"
        iconset.mkdir()
        for pt in (16, 32, 128, 256, 512):
            for mult, suffix in ((1, ""), (2, "@2x")):
                subprocess.run(["sips", "-z", str(pt * mult), str(pt * mult), str(master),
                                "--out", str(iconset / ("icon_%dx%d%s.png" % (pt, pt, suffix)))],
                               check=True, capture_output=True, timeout=30)
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(out)],
                       check=True, capture_output=True, timeout=30)
    return out


def write_assets():
    write_svgs()
    for px, path in MENUBAR_PNGS.items():
        render_menubar_png(px, path)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--assets", action="store_true",
                    help="régénère les SVG et les gabarits de la barre des menus")
    args = ap.parse_args(argv)
    if args.assets:
        write_assets()
        for p in (WORDMARK_SVG, SYMBOL_SVG, *MENUBAR_PNGS.values()):
            print("%s (%d octets)" % (os.path.relpath(p, ROOT), p.stat().st_size))
        return
    out = build_icns()
    print("%s (%d octets)" % (os.path.relpath(out, ROOT), out.stat().st_size))


if __name__ == "__main__":
    main()
