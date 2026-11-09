#!/usr/bin/env python3
"""
Vlocal — Pilule de dictée flottante (NSPanel non-activant + WKWebView).

Une pilule discrète vit en bas de l'écran, centrée au-dessus du Dock. À chaque
dictée, elle se place sur l'écran où se trouve la souris, s'étend et montre
l'onde de la voix, puis le résultat ; le reste du temps elle se replie au repos.
Elle ne vole JAMAIS le focus :
  - NSWindowStyleMaskNonactivatingPanel + canBecomeKey/Main = NO
  - orderFrontRegardless (jamais makeKeyAndOrderFront_)
  - setIgnoresMouseEvents_(True) -> clics qui traversent (ne bloque pas l'app active)

Le panneau est de taille FIXE (PANEL_W x PANEL_H), juste assez pour la plus
grande carte et son ombre : tous les changements de taille de la pilule sont
des transitions CSS dans dictation_overlay.html, jamais un setFrame par image.
Il n'est repositionné qu'une fois par appui (recording()).

Niveau de fenêtre : NSStatusWindowLevel (25), au-dessus du Dock (20). Le cadre
utile (visibleFrame) exclut le Dock au repos, mais un Dock qui s'agrandit au
survol ou qui sort alors qu'il est masqué déborde ce cadre : à un niveau
inférieur il recouvrirait la pilule. Les menus (101) restent au-dessus d'elle.

macOS uniquement. À créer sur le MAIN THREAD Cocoa via create(), qui dispatche
par NSOperationQueue.mainQueue(). Toutes les commandes (show/js) sont elles aussi
marshalées sur le main thread. Le JS est mis en file tant que la page n'a pas
fini de charger (didFinishNavigation).

API de pilotage (no-op si overlay indisponible) :
  create(), recording(), level(v), transcribing(rec_s),
  result(trans_s, inserted, branch, text), too_short(), too_short_tap(),
  locked(label), error(msg), mic_needed(), info(title, sub), fade_out(),
  reset(), show(), hide(), set_theme(t), set_lang(l), set_hotkey_label(l),
  set_anchor_provider(fn) (sans effet, voir sa docstring)

La position se calcule par pill_frame(), fonction pure testée sans AppKit.
"""

import json
import os
import sys

_IS_MAC = sys.platform == "darwin"
_overlay = None  # instance unique (_Overlay)

# Panneau : la carte la plus large (320 px) et son ombre, la consigne posée
# au-dessus de la pilule (dictation_overlay.html : .card, .hint, .pill).
PANEL_W = 360.0
PANEL_H = 132.0
# Écart entre le bas du cadre utile (au-dessus du Dock) et le bas du panneau ;
# la pilule est elle-même à 14 px du bas du panneau, soit 18 px au-dessus du Dock.
BOTTOM_GAP = 4.0


def pill_frame(mouse, screens, size=(PANEL_W, PANEL_H), gap=BOTTOM_GAP):
    """Cadre (x, y, w, h) du panneau, en coordonnées écran Cocoa (origine en bas
    à gauche de l'écran principal, y vers le haut).

    mouse   : (x, y) de la souris (NSEvent.mouseLocation()).
    screens : [(frame, visible_frame), ...] avec frame = (x, y, w, h), l'écran
              principal en premier (ordre de NSScreen.screens()).

    Écran retenu : celui qui contient la souris ; à défaut (souris sur une
    bordure, écran débranché entre-temps), le plus proche ; sans écran, None.
    Le panneau est centré en bas du cadre utile de cet écran et n'en sort pas."""
    if not screens:
        return None
    mx, my = mouse

    def dist2(fr):
        x, y, w, h = fr
        dx = max(x - mx, 0.0, mx - (x + w))
        dy = max(y - my, 0.0, my - (y + h))
        return dx * dx + dy * dy
    frame, vf = min(screens, key=lambda s: dist2(s[0]))   # premier à égalité : principal
    vx, vy, vw, vh = vf
    w, h = size
    x = vx + (vw - w) / 2.0
    x = max(vx, min(x, vx + vw - w))
    y = vy + gap
    y = max(vy, min(y, vy + vh - h))
    return (x, y, w, h)


def set_anchor_provider(fn):
    """Sans effet : la pilule ne s'ancre plus sous l'icône de la barre des menus.
    Gardée parce que app.py l'appelle encore ; à retirer avec cet appel."""
    del fn


def _html_path():
    """dictation_overlay.html du bundle (dev : à côté de ce fichier)."""
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "dictation_overlay.html")


if _IS_MAC:
    import objc
    from AppKit import (
        NSPanel, NSColor, NSScreen, NSEvent,
        NSWindowStyleMaskBorderless, NSWindowStyleMaskNonactivatingPanel,
        NSBackingStoreBuffered, NSStatusWindowLevel,
        NSWindowCollectionBehaviorCanJoinAllSpaces,
        NSWindowCollectionBehaviorStationary,
        NSWindowCollectionBehaviorFullScreenAuxiliary,
        NSWindowCollectionBehaviorIgnoresCycle,
    )
    from Foundation import NSMakeRect, NSObject, NSURL, NSOperationQueue
    import WebKit

    def _on_main(fn):
        """Exécute fn sur le main thread Cocoa (best-effort)."""
        try:
            NSOperationQueue.mainQueue().addOperationWithBlock_(fn)
        except Exception:
            try:
                fn()
            except Exception:
                pass

    def _rect(r):
        return (float(r.origin.x), float(r.origin.y), float(r.size.width), float(r.size.height))

    def _current_frame():
        """Cadre du panneau sur l'écran de la souris (repli : écran principal)."""
        screens = [(_rect(s.frame()), _rect(s.visibleFrame())) for s in (NSScreen.screens() or [])]
        loc = NSEvent.mouseLocation()
        return pill_frame((float(loc.x), float(loc.y)), screens)

    class _OverlayPanel(NSPanel):
        # Ne prend JAMAIS le focus clavier ni ne devient fenêtre principale.
        def canBecomeKeyWindow(self):
            return False

        def canBecomeMainWindow(self):
            return False

    class _NavDelegate(NSObject):
        def initWithOwner_(self, owner):
            self = objc.super(_NavDelegate, self).init()
            if self is None:
                return None
            # Réf forte vers _Overlay (cycle volontaire, cf. _Overlay.__init__).
            self._owner = owner
            return self

        def webView_didFinishNavigation_(self, web, nav):
            try:
                self._owner._on_loaded()
            except Exception:
                pass

        # Échecs de navigation : _ready resterait False à vie (la file JS est
        # bornée par ailleurs) — on LOGGE pour rendre le mode dégradé visible.
        def webView_didFailNavigation_withError_(self, web, nav, error):
            try:
                print(f"[overlay] chargement HTML échoué : {error}")
            except Exception:
                pass

        def webView_didFailProvisionalNavigation_withError_(self, web, nav, error):
            try:
                print(f"[overlay] chargement HTML échoué (provisoire) : {error}")
            except Exception:
                pass

    class _Overlay:
        def __init__(self):
            html_path = _html_path()
            self._ready = False
            self._queue = []
            style = NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel
            x, y, w, h = _current_frame() or (0.0, 0.0, PANEL_W, PANEL_H)
            panel = _OverlayPanel.alloc().initWithContentRect_styleMask_backing_defer_(
                NSMakeRect(x, y, w, h), style, NSBackingStoreBuffered, False)
            panel.setReleasedWhenClosed_(False)
            panel.setLevel_(NSStatusWindowLevel)       # au-dessus du Dock (docstring)
            panel.setCollectionBehavior_(
                NSWindowCollectionBehaviorCanJoinAllSpaces
                | NSWindowCollectionBehaviorStationary
                | NSWindowCollectionBehaviorFullScreenAuxiliary
                | NSWindowCollectionBehaviorIgnoresCycle)
            panel.setOpaque_(False)
            panel.setBackgroundColor_(NSColor.clearColor())
            panel.setHasShadow_(False)                 # l'ombre est dessinée en CSS
            panel.setHidesOnDeactivate_(False)
            try:
                panel.setBecomesKeyOnlyIfNeeded_(True)
            except Exception:
                pass
            # Pur retour visuel : les clics traversent vers l'app active, même
            # sur la pilule (un clic pour dicter demanderait un pont vers app.py).
            panel.setIgnoresMouseEvents_(True)

            cfg = WebKit.WKWebViewConfiguration.alloc().init()
            web = WebKit.WKWebView.alloc().initWithFrame_configuration_(
                NSMakeRect(0, 0, w, h), cfg)
            try:
                web.setValue_forKey_(False, "drawsBackground")
            except Exception:
                pass
            try:
                web.setOpaque_(False)  # NSView — double protection transparence
            except Exception:
                pass
            # Réf forte NÉCESSAIRE : WKWebView ne retient son navigationDelegate
            # que faiblement. Forme un cycle de réfs fortes _Overlay <->
            # _NavDelegate (via _owner), VOLONTAIRE et borné : l'overlay est un
            # singleton à durée de vie du processus, jamais détruit.
            self._nav = _NavDelegate.alloc().initWithOwner_(self)   # réf forte
            web.setNavigationDelegate_(self._nav)
            panel.setContentView_(web)
            if os.path.exists(html_path):
                url = NSURL.fileURLWithPath_(html_path)
                base = NSURL.fileURLWithPath_(os.path.dirname(html_path))
                web.loadFileURL_allowingReadAccessToURL_(url, base)
            else:
                print(f"[overlay] HTML introuvable : {html_path}")
            self.panel = panel
            self.web = web
            # Panneau TOUJOURS présent (la pilule de repos y vit) : jamais
            # d'orderOut, qui rendait le ré-affichage capricieux.
            panel.orderFrontRegardless()

        def _on_loaded(self):
            self._ready = True
            pending, self._queue = self._queue, []
            for code in pending:
                self._eval(code)

        def _eval(self, code):
            try:
                self.web.evaluateJavaScript_completionHandler_(code, None)
            except Exception:
                pass

        def js(self, code):
            def _run():
                if self._ready:
                    self._eval(code)
                else:
                    # Page pas encore chargée : on met en file, SAUF les trames
                    # de niveau micro (transitoires, inutiles à rejouer), et en
                    # BORNANT la file (si la page ne charge jamais, la file ne
                    # doit pas croître sans limite pendant la session).
                    if code.startswith("ovLevel("):
                        return
                    if len(self._queue) > 256:
                        self._queue.pop(0)
                    self._queue.append(code)
            _on_main(_run)

        def show(self):
            def _r():
                self.panel.setAlphaValue_(1.0)
                self.panel.orderFrontRegardless()
            _on_main(_r)

        def place(self):
            """Pose le panneau en bas de l'écran de la souris (une fois par appui)."""
            def _run():
                try:
                    fr = _current_frame()
                    if fr is not None:
                        self.panel.setFrame_display_(NSMakeRect(*fr), False)
                except Exception as e:
                    print(f"[overlay] placement impossible : {e}")
            _on_main(_run)


# --------------------------------------------------------------------------- #
# API publique (no-op silencieux hors macOS ou si l'overlay n'a pas pu naître)
# --------------------------------------------------------------------------- #
def create():
    """Crée l'overlay sur le main thread (idempotent). Appeler après la fenêtre."""
    if not _IS_MAC or _overlay is not None:
        return

    def _mk():
        global _overlay
        if _overlay is not None:      # deux create() rapprochés = UN seul panneau
            return
        try:
            _overlay = _Overlay()
            print("[overlay] pilule de dictée prête (NSPanel non-activant).")
        except Exception as e:
            print(f"[overlay] création KO : {e}")
            return
        # État posé avant la création (thème, langue, raccourci) : rejoué ici,
        # la page le recevra dès son chargement (file d'attente de _Overlay.js).
        if _theme:
            _js("ovTheme('light')" if _theme == "light" else "ovTheme('dark')")
        if _lang:
            _js("ovLang('%s')" % _lang)
        if _hotkey_label:
            _js("ovHotkey(%s)" % json.dumps(_hotkey_label))
    _on_main(_mk)


def _js(code):
    if _overlay is not None:
        _overlay.js(code)


def show():
    if _overlay is not None:
        _overlay.show()


def hide():
    """Fin de dictée, abandon, figeage, entrée en réunion : tout message ou onde
    disparaît et la pilule revient au repos. Le panneau reste visible pour la
    pilule de repos ; le replié est immédiat (aucune transition à interrompre)."""
    if _overlay is not None:
        _js("ovReset()")
        _overlay.show()


_hotkey_label = None  # libellé du raccourci actif, ex. "Ctrl + Cmd"
_theme = None         # thème courant (dark/light), rejoué si l'overlay n'existe pas encore
_lang = None          # langue courante (fr/en), idem


def set_hotkey_label(label):
    """Pousse le libellé RÉEL du raccourci (consignes « maintiens … »)."""
    global _hotkey_label
    _hotkey_label = (label or "").strip() or None
    if _hotkey_label:
        _js("ovHotkey(%s)" % json.dumps(_hotkey_label))


def set_theme(theme):
    """Thème de la pilule (dark/light), poussé par app.py au démarrage et à chaque
    changement du réglage « Apparence »."""
    global _theme
    _theme = "light" if theme == "light" else "dark"
    _js("ovTheme('light')" if _theme == "light" else "ovTheme('dark')")


def set_lang(lang):
    """Langue de la pilule (fr/en), poussée par app.py.set_lang."""
    global _lang
    _lang = "en" if lang == "en" else "fr"
    _js("ovLang('en')" if _lang == "en" else "ovLang('fr')")


def recording():
    if _overlay is not None:
        _overlay.place()        # écran de la souris, au moment de l'appui
    show()
    _js("ovRecording()")


def level(v):
    _js("ovLevel(%.4f)" % float(v))


def transcribing(rec_s):
    _js("ovTranscribing(%.3f)" % float(rec_s))


def result(trans_s, inserted, branch=None, text=""):
    _js("ovResult(%.3f,%s,%s,%s)" % (float(trans_s),
                                     "true" if inserted else "false",
                                     json.dumps(branch),
                                     json.dumps((text or "")[:240])))


def too_short():
    show()
    _js("ovTooShort()")


def too_short_tap():
    """Appui bref isolé en mode double appui : rien à transcrire, et c'est
    l'occasion d'apprendre le geste (« appuie deux fois »)."""
    show()
    _js("ovTooShortTap()")


def locked(label=None):
    """Micro maintenu ouvert après un double appui : la pilule le dit, et dit
    comment terminer (le raccourci réel, pas un libellé en dur)."""
    _js("ovLocked(%s)" % json.dumps((label or _hotkey_label or "").strip()))


def error(msg=""):
    show()
    _js("ovError(%s)" % json.dumps(msg or ""))


def mic_needed():
    show()
    _js("ovMicNeeded()")


def info(title, sub):
    """Carte d'information générique (ex. permission à accorder)."""
    show()
    _js("ovInfo(%s,%s)" % (json.dumps(title or ""), json.dumps(sub or "")))


def fade_out():
    _js("ovFadeOut()")


def reset():
    _js("ovReset()")
