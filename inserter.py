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
"""

import sys
import threading
import time

import clipboard

_IS_MAC = sys.platform == "darwin"


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
        return False


def insert_at_cursor(text: str, restore_clipboard: bool = True, original=None) -> bool:
    """Colle `text` à la position du curseur dans l'app active.
    Préserve le presse-papier (restauration ~0,9 s après). True si collage posté.

    `original` : contenu du presse-papier AVANT que l'appelant n'y ait copié le
    texte dicté (INS-1). Sans lui, on relit le presse-papier ici — mais il a
    souvent déjà été écrasé par le texte dicté, donc on « restaurait » le texte
    dicté au lieu de l'original. Fournir `original` corrige ce bug."""
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
        pass
    # INS-5 — Saisie sécurisée active (champ mot de passe...) : macOS avale le
    # Cmd+V simulé SANS erreur. On NE tente PAS le collage (sinon faux succès +
    # le texte serait effacé par la restauration). Texte laissé au presse-papier
    # (repli Cmd+V) et on signale l'échec -> l'appelant affiche l'invite.
    try:
        if _secure_input_active():
            clipboard.set_clipboard(text)
            return False
    except Exception:
        pass
    saved = (original if original is not None
             else clipboard.get_clipboard()) if restore_clipboard else None
    if not clipboard.set_clipboard(text):
        return False
    # petite pause pour que le presse-papier soit bien à jour avant le collage
    time.sleep(0.05)
    if not _paste_keystroke():
        return False  # texte laissé dans le presse-papier (repli)
    # INS-2 : `saved` truthy (pas seulement non-None) -> on ne réécrit JAMAIS une
    # chaîne vide (qui détruirait une image/un fichier copié, non lisible en texte).
    if restore_clipboard and saved:
        def _restore():
            time.sleep(0.9)   # INS-3 : marge élargie pour les apps lentes à coller
            # On ne restaure QUE si le presse-papier contient encore notre texte
            # (sinon l'utilisateur a copié autre chose entre-temps : on respecte).
            if clipboard.get_clipboard() == text:
                clipboard.set_clipboard(saved)
        threading.Thread(target=_restore, daemon=True).start()
    return True
