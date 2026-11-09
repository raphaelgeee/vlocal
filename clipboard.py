#!/usr/bin/env python3
"""
Vlocal — Presse-papier macOS.

macOS : NSPasteboard (primaire, lecture+écriture, Unicode sûr) ;
repli pbcopy/pbpaste avec locale UTF-8 forcée.

Aucune dépendance obligatoire. Best-effort : un échec n'interrompt jamais la
dictée, il est journalisé en DEBUG.

Fiabilité #10 — instantané COMPLET : snapshot() capture tous les types de
chaque élément du presse-papier (texte, image, fichiers copiés...) et
restore() les rend tels quels. On retient aussi le compteur de changements
(changeCount) après NOTRE dernière écriture : is_own_content() dit si le
presse-papier contient encore ce que Vlocal y a mis (personne n'a copié
autre chose depuis).
"""

import subprocess
import sys

import applog

log = applog.get("clipboard")

_IS_MAC = sys.platform == "darwin"
_TEXT_TYPE = "public.utf8-plain-text"   # = NSPasteboardTypeString
_own_change = [None]                    # changeCount après notre dernière écriture


def _pasteboard():
    from AppKit import NSPasteboard
    return NSPasteboard.generalPasteboard()


def _remember_own_write(pb=None) -> None:
    try:
        _own_change[0] = int((pb or _pasteboard()).changeCount())
    except Exception:
        log.debug("changeCount illisible", exc_info=True)
        _own_change[0] = None


def _mac_set_native(text: str) -> bool:
    """Presse-papier via NSPasteboard (Unicode CORRECT, indépendant de la locale).
    pbcopy en .app tourne sans locale UTF-8 -> il mé-décode les accents
    (« réunion » -> « r√©union »). NSPasteboard reçoit un NSString Unicode : zéro
    corruption, quels que soient les accents/emojis."""
    from AppKit import NSPasteboardTypeString
    pb = _pasteboard()
    pb.clearContents()
    ok = bool(pb.setString_forType_(text, NSPasteboardTypeString))
    if ok:
        _remember_own_write(pb)
    return ok


def _utf8_env():
    import os
    env = dict(os.environ)
    env["LANG"] = "en_US.UTF-8"
    env["LC_CTYPE"] = "UTF-8"
    return env


def set_clipboard(text: str) -> bool:
    """Place `text` dans le presse-papier. True si OK."""
    if text is None:
        text = ""
    if not _IS_MAC:
        return False
    try:
        if _mac_set_native(text):
            return True
    except Exception:
        log.debug("NSPasteboard indisponible -> repli pbcopy", exc_info=True)
    try:
        # Repli pbcopy AVEC locale UTF-8 forcée (sinon mojibake des accents).
        subprocess.run(["pbcopy"], input=text.encode("utf-8"),
                       check=True, timeout=3, env=_utf8_env())
    except Exception:
        log.debug("pbcopy KO", exc_info=True)
        return False
    _remember_own_write()
    return True


def get_clipboard() -> str:
    """Lit le texte du presse-papier ("" s'il n'y en a pas)."""
    if not _IS_MAC:
        return ""
    try:
        from AppKit import NSPasteboardTypeString
        s = _pasteboard().stringForType_(NSPasteboardTypeString)
        if s is not None:
            return str(s)
    except Exception:
        log.debug("NSPasteboard illisible -> repli pbpaste", exc_info=True)
    try:
        r = subprocess.run(["pbpaste"], capture_output=True, timeout=3, env=_utf8_env())
        return r.stdout.decode("utf-8", "replace")
    except Exception:
        log.debug("pbpaste KO", exc_info=True)
        return ""


def is_own_content() -> bool:
    """True si le presse-papier n'a pas changé depuis la dernière écriture de
    Vlocal (texte dicté ou restauration). False dans le doute."""
    if _own_change[0] is None:
        return False
    try:
        return int(_pasteboard().changeCount()) == _own_change[0]
    except Exception:
        log.debug("changeCount illisible", exc_info=True)
        return False


def snapshot():
    """Instantané COMPLET du presse-papier : tuple d'éléments, chacun tuple de
    (type, octets). Tuple vide = presse-papier vide. Sans AppKit, repli sur le
    texte seul."""
    if not _IS_MAC:
        return ()
    try:
        items = []
        for item in (_pasteboard().pasteboardItems() or ()):
            reps = []
            for t in (item.types() or ()):
                data = item.dataForType_(t)
                if data is not None:
                    reps.append((str(t), bytes(data)))
            if reps:
                items.append(tuple(reps))
        return tuple(items)
    except Exception:
        log.debug("instantané complet KO -> texte seul", exc_info=True)
    text = get_clipboard()
    return (((_TEXT_TYPE, text.encode("utf-8")),),) if text else ()


def restore(snap) -> bool:
    """Rend au presse-papier un instantané de snapshot(). True si OK."""
    if not snap or not _IS_MAC:
        return False
    try:
        from AppKit import NSPasteboardItem
        from Foundation import NSData
        pb = _pasteboard()
        objs = []
        for reps in snap:
            item = NSPasteboardItem.alloc().init()
            for t, data in reps:
                item.setData_forType_(NSData.dataWithBytes_length_(data, len(data)), t)
            objs.append(item)
        pb.clearContents()
        if pb.writeObjects_(objs):
            _remember_own_write(pb)
            return True
        log.debug("writeObjects_ refusé -> texte seul")
    except Exception:
        log.debug("restauration complète KO -> texte seul", exc_info=True)
    for reps in snap:
        for t, data in reps:
            if t == _TEXT_TYPE:
                return set_clipboard(data.decode("utf-8", "replace"))
    return False
