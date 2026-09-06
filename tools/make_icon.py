#!/usr/bin/env python3
"""Génère build/AppIcon.icns : V blanc sur carré charbon arrondi (rendu pyobjc
off-screen, sans NSWindow). Sert d'icône de l'app empaquetée."""
import os, subprocess, sys, tempfile
from pathlib import Path
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
GLYPH = ROOT / "assets" / "vocal-glyph-v-36.png"
OUT_PNG = HERE / "_appicon_1024.png"
OUT_ICNS = HERE / "AppIcon.icns"

def render_png():
    from AppKit import (NSBitmapImageRep, NSGraphicsContext, NSColor, NSImage,
                        NSBezierPath, NSCompositingOperationSourceOver,
                        NSDeviceRGBColorSpace)
    from Foundation import NSMakeRect
    SZ = 1024
    rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, SZ, SZ, 8, 4, True, False, NSDeviceRGBColorSpace, 0, 0)
    ctx = NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    NSGraphicsContext.saveGraphicsState(); NSGraphicsContext.setCurrentContext_(ctx)
    inset = SZ*0.06
    rect = NSMakeRect(inset, inset, SZ-2*inset, SZ-2*inset)
    radius = (SZ-2*inset)*0.225
    NSColor.colorWithCalibratedRed_green_blue_alpha_(17/255,17/255,19/255,1.0).set()   # v3.2.8 — charbon NEUTRE (plus de bleu)
    NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(rect, radius, radius).fill()
    glyph = NSImage.alloc().initWithContentsOfFile_(str(GLYPH))
    if glyph is not None:
        from AppKit import NSRectFillUsingOperation, NSCompositingOperationSourceAtop
        gw=(SZ-2*inset)*0.62; g=(SZ-gw)/2   # v3.2.8 — V plus grand : il ressort mieux dans le Dock
        # v3.2.8 — GLYPH EN BLANC. Le PNG source est NOIR : dessiné tel quel il serait
        # noir-sur-charbon (invisible, "ne ressort pas"). On le teinte BLANC dans une
        # image ISOLÉE (SourceAtop ne touche que les pixels opaques du glyph), puis on
        # la pose sur le carré charbon -> logo blanc net qui ressort.
        gsz = glyph.size()
        gr = NSMakeRect(0, 0, gsz.width, gsz.height)
        white_glyph = NSImage.alloc().initWithSize_(gsz)
        white_glyph.lockFocus()
        glyph.drawInRect_fromRect_operation_fraction_(gr, gr, NSCompositingOperationSourceOver, 1.0)
        NSColor.whiteColor().set()
        NSRectFillUsingOperation(gr, NSCompositingOperationSourceAtop)
        white_glyph.unlockFocus()
        white_glyph.drawInRect_fromRect_operation_fraction_(
            NSMakeRect(g,g,gw,gw), gr, NSCompositingOperationSourceOver, 1.0)
    NSGraphicsContext.restoreGraphicsState()
    png = rep.representationUsingType_properties_(4, None)
    png.writeToFile_atomically_(str(OUT_PNG), True)

def build_icns():
    iconset = Path(tempfile.mkdtemp())/ "AppIcon.iconset"; iconset.mkdir(parents=True)
    sizes = {"icon_16x16.png":16,"icon_16x16@2x.png":32,"icon_32x32.png":32,
             "icon_32x32@2x.png":64,"icon_128x128.png":128,"icon_128x128@2x.png":256,
             "icon_256x256.png":256,"icon_256x256@2x.png":512,"icon_512x512.png":512,
             "icon_512x512@2x.png":1024}
    for name,sz in sizes.items():
        subprocess.run(["sips","-z",str(sz),str(sz),str(OUT_PNG),"--out",str(iconset/name)],
                       check=True, capture_output=True)
    subprocess.run(["iconutil","-c","icns",str(iconset),"-o",str(OUT_ICNS)], check=True)

if __name__ == "__main__":
    render_png(); build_icns()
    print("AppIcon.icns:", OUT_ICNS.exists(), OUT_ICNS.stat().st_size, "octets")
