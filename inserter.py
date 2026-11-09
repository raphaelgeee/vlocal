#!/usr/bin/env python3
"""
Vlocal — Insertion au curseur macOS (« type into any app », façon Wispr).

Après une dictée au raccourci global, on écrit le texte LÀ OÙ EST LE CURSEUR
dans l'app active (Notion, Mail, Slack, Cursor, Word…).

Méthode : presse-papier + raccourci de collage simulé (Cmd+V).
Le collage (vs frappe caractère par caractère) est le plus fiable pour
l'Unicode/accents/emojis et le texte long.

On préserve le presse-papier de l'utilisateur (sauvegarde + restauration
différée). Prérequis : permission « Accessibilité ».
Si le collage échoue, le texte reste dans le presse-papier (repli garanti).

Fiabilité #10 — CHAÎNE DE DICTÉES : begin_chain() est appelé à CHAQUE appui
du raccourci. Il prend un instantané complet du presse-papier (texte, image,
fichiers) sauf si le presse-papier contient encore ce que Vlocal y a mis
(dictée précédente non encore rendue, ou déjà rendue) : on garde alors
l'instantané du PREMIER appui de la chaîne. Seule la dernière insertion de la
chaîne restaure, et seulement si personne n'a copié autre chose entre-temps.
"""

import sys
import threading
import time

import applog
import clipboard

log = applog.get("inserter")

_IS_MAC = sys.platform == "darwin"
RESTORE_DELAY_S = 0.9   # INS-3 : marge pour les apps lentes à coller

# Chaîne en cours : instantané d'origine (None = aucun) et numéro de la
# dernière insertion (seule celle-ci restaure). Protégé par _chain_lock.
_chain = {"snap": None, "gen": 0}
_chain_lock = threading.Lock()


def begin_chain() -> None:
    """Appui du raccourci : capture le presse-papier d'origine, sauf si la
    chaîne en cours l'a déjà (le presse-papier est encore le nôtre)."""
    with _chain_lock:
        if _chain["snap"] is not None and clipboard.is_own_content():
            return
    snap = clipboard.snapshot()
    with _chain_lock:
        _chain["snap"] = snap


def _paste_keystroke() -> bool:
    """Simule Cmd+V (macOS) via événements clavier NATIFS (Quartz CGEvent,
    keycode brut). PAS pynput : pynput mappe le caractère via TSM et crashe
    l'app hors main thread. Keycode V = 9 (kVK_ANSI_V), indépendant du layout."""
    if _IS_MAC:
        try:
            import Quartz
            VK_V = 9
            down = Quartz.CGEventCreateKeyboardEvent(None, VK_V, True)
            Quartz.CGEventSetFlags(down, Quartz.kCGEventFlagMaskCommand)
            up = Quartz.CGEventCreateKeyboardEvent(None, VK_V, False)
            Quartz.CGEventSetFlags(up, Quartz.kCGEventFlagMaskCommand)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)
            return True
        except Exception:
            log.warning("[insert] Cmd+V simulé KO", exc_info=True)
            return False
    return False


def _secure_input_active() -> bool:
    """True si la SAISIE SÉCURISÉE macOS est active (champ mot de passe, etc.).
    Dans ce cas macOS AVALE tout Cmd+V simulé SANS erreur -> _paste_keystroke
    renverrait un FAUX succès, puis la restauration différée effacerait le texte
    (presse-papier inchangé == notre texte). On détecte donc l'état EN AMONT pour
    ne pas tenter le collage. Carbon.IsSecureEventInputEnabled = fonction C simple
    (Booléen). En cas de doute (exception) -> False (on ne bloque pas le collage)."""
    if not _IS_MAC:
        return False
    try:
        import ctypes
        carbon = ctypes.CDLL("/System/Library/Frameworks/Carbon.framework/Carbon")
        return bool(carbon.IsSecureEventInputEnabled())
    except Exception:
        log.debug("IsSecureEventInputEnabled illisible", exc_info=True)
        return False


def insert_at_cursor(text: str, restore_clipboard: bool = True) -> bool:
    """Colle `text` à la position du curseur dans l'app active.
    Rend le presse-papier d'origine ~0,9 s après. True si collage posté.

    L'origine est l'instantané de la chaîne (begin_chain, pris à l'appui, AVANT
    que la dictée n'écrive son texte au presse-papier : INS-1). Sans chaîne
    ouverte, on la capture ici."""
    if not text:
        return False
    # INS-4 — Sans permission Accessibilité, le Cmd+V simulé (CGEventPost) est
    # AVALÉ par macOS SANS erreur : on croirait avoir collé (return True), puis la
    # restauration différée effacerait le texte du presse-papier => texte PERDU
    # silencieusement (ni au curseur, ni copié). On teste la permission AVANT : si
    # absente, on laisse le texte dans le presse-papier (repli Cmd+V manuel) et on
    # renvoie False -> l'appelant affiche l'invite « collez avec Cmd+V + autorisez
    # l'Accessibilité ». (accessibility_ok est LIVE : dès l'octroi, l'insertion
    # auto repart sans relancer l'app.)
    try:
        import permissions
        if not permissions.accessibility_ok():
            clipboard.set_clipboard(text)   # texte dispo pour un Cmd+V manuel
            return False
    except Exception:
        log.debug("permissions.accessibility_ok : échec ignoré", exc_info=True)
    # INS-5 — Saisie sécurisée active (champ mot de passe...) : macOS avale le
    # Cmd+V simulé SANS erreur. On NE tente PAS le collage (sinon faux succès +
    # le texte serait effacé par la restauration). Texte laissé au presse-papier
    # (repli Cmd+V) et on signale l'échec -> l'appelant affiche l'invite.
    if _secure_input_active():
        clipboard.set_clipboard(text)
        return False
    if restore_clipboard:
        with _chain_lock:
            opened = _chain["snap"] is not None
        if not opened:
            begin_chain()
    if not clipboard.set_clipboard(text):
        return False
    # petite pause pour que le presse-papier soit bien à jour avant le collage
    time.sleep(0.05)
    if not _paste_keystroke():
        return False  # texte laissé dans le presse-papier (repli)
    if restore_clipboard:
        with _chain_lock:
            _chain["gen"] += 1
            my_gen = _chain["gen"]
        t = threading.Timer(RESTORE_DELAY_S, _restore_origin, args=(my_gen,))
        t.daemon = True
        t.start()
    return True


def _restore_origin(my_gen) -> None:
    """Fin de chaîne : rend l'origine si cette insertion est la dernière et si
    le presse-papier est encore le nôtre (sinon l'utilisateur a copié autre
    chose : on respecte). INS-2 : origine vide = on laisse le texte dicté."""
    with _chain_lock:
        if my_gen != _chain["gen"]:
            return          # une insertion plus récente de la chaîne restaurera
        snap = _chain["snap"]
    if snap and clipboard.is_own_content():
        if not clipboard.restore(snap):
            log.warning("[insert] presse-papier d'origine non restauré")
