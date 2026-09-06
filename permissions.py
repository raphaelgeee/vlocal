#!/usr/bin/env python3
"""
Vlocal — Détection des permissions macOS (TCC), 100 % local, lecture seule.

Deux permissions concernent Vlocal :
  - Microphone : capter la voix (AVFoundation).
  - Accessibilité : moniteur NSEvent GLOBAL du raccourci (hotkey_mac.py) ET
    insertion automatique au curseur (inserter.py / Cmd+V simulé).

Aucune dépendance hors pyobjc déjà présent. Toutes les fonctions sont
best-effort : jamais d'exception propagée (statut neutre/permissif en cas
d'échec). macOS uniquement ; ailleurs tout est considéré « OK » (pas de TCC).
"""

import sys

_IS_MAC = sys.platform == "darwin"

# Statuts micro AVFoundation (authorizationStatusForMediaType:) — enum complète :
# 0 = notDetermined, 1 = restricted, 2 = denied, 3 = authorized.
MIC_AUTHORIZED = 3


def _safe(fn, default):
    try:
        return fn()
    except Exception:
        return default


# --- Accessibilité (insertion au curseur) --------------------------------- #
def accessibility_ok() -> bool:
    if not _IS_MAC:
        return True

    def _f():
        from ApplicationServices import AXIsProcessTrusted
        return bool(AXIsProcessTrusted())
    return _safe(_f, True)


def accessibility_prompt():
    """Déclenche le dialogue système d'autorisation (si non encore accordé)."""
    if not _IS_MAC:
        return

    def _f():
        from ApplicationServices import AXIsProcessTrustedWithOptions
        # Clé littérale (évite l'import du symbole kAXTrustedCheckOptionPrompt).
        AXIsProcessTrustedWithOptions({"AXTrustedCheckOptionPrompt": True})
    _safe(_f, None)


# --- Microphone ------------------------------------------------------------ #
def mic_status() -> int:
    if not _IS_MAC:
        return MIC_AUTHORIZED

    def _f():
        import objc
        d = {}
        objc.loadBundle("AVFoundation", d,
                        bundle_path="/System/Library/Frameworks/AVFoundation.framework")
        AVCaptureDevice = objc.lookUpClass("AVCaptureDevice")
        # 'soun' == AVMediaTypeAudio (FourCC). Évite d'importer le symbole.
        return int(AVCaptureDevice.authorizationStatusForMediaType_("soun"))
    return _safe(_f, MIC_AUTHORIZED)


def mic_ok() -> bool:
    return mic_status() == MIC_AUTHORIZED


def mic_prompt():
    """v1.0.3 — Déclenche le dialogue système d'autorisation Micro (uniquement si
    le statut est « non déterminé »). N'ouvre AUCUN flux audio : utilise l'API
    native AVFoundation requestAccessForMediaType:. Best-effort, jamais d'exception."""
    if not _IS_MAC:
        return

    def _f():
        import objc
        d = {}
        objc.loadBundle("AVFoundation", d,
                        bundle_path="/System/Library/Frameworks/AVFoundation.framework")
        AVCaptureDevice = objc.lookUpClass("AVCaptureDevice")

        def _handler(granted):   # bloc de complétion (résultat ignoré)
            return None
        # 'soun' == AVMediaTypeAudio (FourCC). Le prompt n'apparaît qu'une fois.
        AVCaptureDevice.requestAccessForMediaType_completionHandler_("soun", _handler)
    _safe(_f, None)


# --- Ouvrir le bon panneau des Réglages Système ---------------------------- #
_PANES = {
    "accessibility": "Privacy_Accessibility",
    "microphone": "Privacy_Microphone",
    "screen": "Privacy_ScreenCapture",   # v1.0.12 — permission « Enregistrement de l'écran » (visio)
}


def open_settings(pane: str = "accessibility") -> bool:
    """Ouvre Réglages Système sur le panneau Confidentialité demandé."""
    if not _IS_MAC:
        return False
    anchor = _PANES.get(pane, "Privacy_Accessibility")

    def _f():
        from AppKit import NSWorkspace
        from Foundation import NSURL
        url = NSURL.URLWithString_(
            "x-apple.systempreferences:com.apple.preference.security?" + anchor)
        return bool(NSWorkspace.sharedWorkspace().openURL_(url))
    return _safe(_f, False)
