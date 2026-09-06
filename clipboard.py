#!/usr/bin/env python3
"""
Vlocal — Presse-papier macOS.

macOS : NSPasteboard (primaire, lecture+écriture, Unicode sûr) ;
repli pbcopy/pbpaste avec locale UTF-8 forcée.

Aucune dépendance obligatoire. Tout échec est silencieux (best-effort).
"""

import subprocess
import sys

_IS_MAC = sys.platform == "darwin"


def _mac_set_native(text: str) -> bool:
    """Presse-papier via NSPasteboard (Unicode CORRECT, indépendant de la locale).
    pbcopy en .app tourne sans locale UTF-8 -> il mé-décode les accents
    (« réunion » -> « r√©union »). NSPasteboard reçoit un NSString Unicode : zéro
    corruption, quels que soient les accents/emojis."""
    from AppKit import NSPasteboard, NSPasteboardTypeString
    pb = NSPasteboard.generalPasteboard()
    pb.clearContents()
    return bool(pb.setString_forType_(text, NSPasteboardTypeString))


def set_clipboard(text: str) -> bool:
    """Place `text` dans le presse-papier. True si OK."""
    if text is None:
        text = ""
    try:
        if _IS_MAC:
            try:
                if _mac_set_native(text):
                    return True
            except Exception:
                pass
            # Repli pbcopy AVEC locale UTF-8 forcée (sinon mojibake des accents).
            import os
            env = dict(os.environ)
            env["LANG"] = "en_US.UTF-8"
            env["LC_CTYPE"] = "UTF-8"
            subprocess.run(["pbcopy"], input=text.encode("utf-8"),
                           check=True, timeout=3, env=env)
            return True
    except Exception:
        pass
    return False


def get_clipboard() -> str:
    """Lit le presse-papier (pour le préserver lors d'une insertion)."""
    try:
        if _IS_MAC:
            try:
                from AppKit import NSPasteboard, NSPasteboardTypeString
                s = NSPasteboard.generalPasteboard().stringForType_(NSPasteboardTypeString)
                if s is not None:
                    return str(s)
            except Exception:
                pass
            import os
            env = dict(os.environ)
            env["LANG"] = "en_US.UTF-8"
            env["LC_CTYPE"] = "UTF-8"
            r = subprocess.run(["pbpaste"], capture_output=True, timeout=3, env=env)
            return r.stdout.decode("utf-8", "replace")
    except Exception:
        pass
    return ""
