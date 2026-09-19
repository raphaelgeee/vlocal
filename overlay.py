#!/usr/bin/env python3
"""
Vlocal — Overlay de dictée flottant (NSPanel non-activant + WKWebView).

HUD liquid glass charbon affiché AU-DESSUS de toutes les apps pendant la dictée
au raccourci global, qui NE VOLE JAMAIS le focus :
  - NSWindowStyleMaskNonactivatingPanel + canBecomeKey/Main = NO
  - orderFrontRegardless (jamais makeKeyAndOrderFront_)
  - setIgnoresMouseEvents_(True) -> clics qui traversent (ne bloque pas l'app active)

Contenu = HTML/CSS (dictation_overlay.html) pour rester cohérent avec la DA et
réutiliser les classes liquid glass. Le panel coexiste avec la fenêtre Vlocal
(c'est une fenêtre séparée).

macOS uniquement. À créer sur le MAIN THREAD Cocoa (cf. menubar.py) via create(),
qui dispatche par NSOperationQueue.mainQueue(). Toutes les commandes (show/js)
sont elles aussi marshalées sur le main thread. Le JS est mis en file tant que la
page n'a pas fini de charger (didFinishNavigation).

API de pilotage (no-op si overlay indisponible) :
  create(), recording(), level(v), transcribing(rec_s),
  result(trans_s, inserted, branch, text), too_short(), error(msg),
  mic_needed(), info(title, sub), fade_out(), reset(), show(),
  set_anchor_provider(fn)
"""

import os
import sys

_IS_MAC = sys.platform == "darwin"
_overlay = None  # instance unique (_Overlay)
_anchor_provider = None  # callable -> (x, y, w, h) de l'icône menu bar (ou None)

# Géométrie verticale de la bulle (dictation_overlay.html) : le corps (.wrap)
# commence à 44 px du haut du panneau (marge pour l'aura) ; le bec (.point, carré
# de 14 px tourné de 45°) déborde de 6 px au-dessus du corps, son sommet est donc
# à 44 - 6 - (7·√2 - 7) ≈ 35 px du haut du panneau. Cf. _Overlay._top_offset.
_TIP_APEX_PX = 35.0


def set_anchor_provider(fn):
    """Fournit la position de l'icône Vlocal de la barre des menus, pour ancrer
    le bec de la bulle dessous. fn() -> (x, y, w, h) en coords écran, ou None."""
    global _anchor_provider
    _anchor_provider = fn


def _base_dir():
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.dirname(os.path.abspath(__file__))


# Version du HTML embarqué : bump quand le HTML du bundle change, pour rafraîchir
# la copie éditable. Permet d'ajuster l'apparence de l'overlay SANS re-signer
# l'app (un re-sign casserait l'autorisation Accessibilité accordée).
_OVERLAY_HTML_VERSION = "11"   # v29.7 — waveform unifiée (48 barres blanches, ampli 54)


def _resolve_html_path():
    """Charge le HTML de l'overlay depuis App Support (copie éditable hors bundle).
    On copie depuis le bundle au 1er lancement et à chaque changement de version ;
    sinon on garde la copie locale (ajustements manuels possibles, zéro rebuild)."""
    bundle_html = os.path.join(_base_dir(), "dictation_overlay.html")
    support = os.path.expanduser("~/Library/Application Support/Vlocal")
    user_html = os.path.join(support, "dictation_overlay.html")
    try:
        os.makedirs(support, exist_ok=True)
        # v3.2.8 — RAFRAÎCHISSEMENT PAR CONTENU (et non par numéro de version qu'on
        # oubliait de bumper -> overlay PÉRIMÉ chez les clients en mise à jour, vu en
        # test : copie locale restée à l'ancien design). On recopie le bundle dès que
        # la copie locale DIFFÈRE : tout changement de design est repris, zéro numéro.
        need_copy = True
        if os.path.exists(user_html):
            try:
                with open(bundle_html, "rb") as a, open(user_html, "rb") as b:
                    need_copy = (a.read() != b.read())
            except Exception:
                need_copy = True
        if need_copy:
            import shutil
            shutil.copyfile(bundle_html, user_html)
    except Exception:
        return bundle_html
    return user_html if os.path.exists(user_html) else bundle_html


_html_path = None  # résolu paresseusement (mémoïsé) par _get_html_path()


def _get_html_path():
    """Résolution PARESSEUSE du HTML (mémoïsée) : l'import du module ne doit
    avoir AUCUN effet de bord (mkdir + copie dans ~/Library), notamment hors
    macOS ou dans un contexte de test qui importe overlay."""
    global _html_path
    if _html_path is None:
        _html_path = _resolve_html_path()
    return _html_path


if _IS_MAC:
    import objc
    from AppKit import (
        NSPanel, NSColor, NSScreen,
        NSWindowStyleMaskBorderless, NSWindowStyleMaskNonactivatingPanel,
        NSBackingStoreBuffered, NSFloatingWindowLevel,
        NSWindowCollectionBehaviorCanJoinAllSpaces,
        NSWindowCollectionBehaviorStationary,
        NSWindowCollectionBehaviorFullScreenAuxiliary,
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

    class _OverlayPanel(NSPanel):
        # Ne prend JAMAIS le focus clavier ni ne devient fenêtre principale.
        def canBecomeKeyWindow(self):
            return False

        def canBecomeMainWindow(self):
            return False

        # v1.0.3 — DÉSACTIVE le clamp macOS : par défaut AppKit force une fenêtre
        # à rester SOUS la barre des menus (constrainFrameRect). C'est ce clamp
        # qui collait l'overlay ~50px trop bas quel que soit l'offset. En rendant
        # le rect tel quel, le panneau peut déborder au-dessus de la barre : la
        # pilule remonte snug et son aura passe DERRIÈRE la barre (niveau flottant
        # < niveau menu bar -> occlusion propre, pas de cadre).
        def constrainFrameRect_toScreen_(self, frameRect, screen):
            return frameRect

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
        # Panel = plus large/haut que la pilule pour laisser respirer le halo
        # iridescent + la drop-shadow. La bulle (.wrap) fait WRAP_W, centrée ->
        # marge (W-WRAP_W)/2 de chaque côté.
        # v3.2.9 — design PILULE : pilule/carte = 404px ; halo ~23px de débord +
        # ombre ~46px sous la carte résultat (2 lignes) -> panneau élargi/agrandi.
        # v1.0.1 fix « cadre » : panneau ÉLARGI (580x300) pour que l'aura iridescente
        # ait la place de se fondre à ZÉRO avant le bord -> plus de limite nette
        # (l'ancien 480x230 rognait le flou = ligne visible = « effet cadre »).
        W = 580
        H = 300
        WRAP_W = 404

        @staticmethod
        def _top_offset(scr, vf):
            """v1.1.0 — décalage vertical CONSTANT : le sommet du bec vient se poser
            1 px sous le bord de la barre des menus, quelle que soit sa hauteur.

            L'ancien calcul (v1.0.9) supposait une barre de 37 px et remontait la
            bulle de la différence : sur un 14" à encoche (barre 33 px) le bec
            remontait de 4 px SOUS la barre, où il était masqué (« engloutie ») ;
            sur un 13" sans encoche (24 px) il disparaissait entièrement. Le
            panneau est positionné par rapport à visibleFrame, dont le haut EST le
            bas de la barre des menus : la distance bec / barre ne dépend donc pas
            de la hauteur de la barre, il n'y a rien à compenser."""
            return _TIP_APEX_PX - 1.0

        def __init__(self):
            # Résolution UNIQUE du HTML (mémoïsée), sur le main thread, avant
            # le loadFileURL_ — plus d'effet de bord à l'import du module.
            html_path = _get_html_path()
            self._ready = False
            self._queue = []
            style = NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel
            scr = NSScreen.mainScreen()
            vf = scr.visibleFrame()
            x = vf.origin.x + (vf.size.width - self.W) / 2.0
            # v1.0.9 — offset ADAPTATIF à la hauteur de la barre des menus (14"
            # à encoche = inchangé ; 13" sans encoche = remonté pour rester snug).
            y = (vf.origin.y + vf.size.height) - self.H + self._top_offset(scr, vf)
            rect = NSMakeRect(x, y, self.W, self.H)
            panel = _OverlayPanel.alloc().initWithContentRect_styleMask_backing_defer_(
                rect, style, NSBackingStoreBuffered, False)
            panel.setReleasedWhenClosed_(False)
            panel.setLevel_(NSFloatingWindowLevel)
            panel.setCollectionBehavior_(
                NSWindowCollectionBehaviorCanJoinAllSpaces
                | NSWindowCollectionBehaviorStationary
                | NSWindowCollectionBehaviorFullScreenAuxiliary)
            panel.setOpaque_(False)
            panel.setBackgroundColor_(NSColor.clearColor())
            panel.setHasShadow_(False)
            panel.setHidesOnDeactivate_(False)
            try:
                panel.setBecomesKeyOnlyIfNeeded_(True)
            except Exception:
                pass
            # Overlay de PUR feedback : les clics/la souris traversent vers l'app
            # active (garde-fou : ne jamais bloquer l'app de l'utilisateur).
            panel.setIgnoresMouseEvents_(True)

            cfg = WebKit.WKWebViewConfiguration.alloc().init()
            web = WebKit.WKWebView.alloc().initWithFrame_configuration_(
                NSMakeRect(0, 0, self.W, self.H), cfg)
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
            # Panneau TOUJOURS présent (carte transparente quand inactive) : on ne
            # fait JAMAIS orderOut (qui rendait le ré-affichage capricieux -> le
            # bug « marche une fois »). On bascule seulement le contenu/l'opacité.
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
                self.panel.setAlphaValue_(1.0)   # ré-opacifie (cf. hide() dur)
                self.panel.orderFrontRegardless()
            _on_main(_r)

        def hide(self):
            # v29.2 — MASQUAGE DUR au niveau FENÊTRE (alpha 0), indépendant du
            # contenu JS. Avant, l'overlay se cachait UNIQUEMENT en rendant sa
            # carte HTML transparente (ovFadeOut) ; si une dictée se FIGE ou
            # qu'une transition est interrompue, la carte restait visible et
            # chevauchait la fenêtre principale/réunion (« deux panneaux
            # superposés »). alpha 0 garantit l'invisibilité quoi qu'il arrive ;
            # le panneau reste PRÉSENT (pas d'orderOut, qui rendait le
            # ré-affichage capricieux).
            _on_main(lambda: self.panel.setAlphaValue_(0.0))

        def reposition(self):
            """Place la bulle SOUS l'icône Vlocal de la barre des menus et aligne
            le bec dessus. Repli : haut-droite si l'icône est introuvable."""
            def _run():
                try:
                    scr = NSScreen.mainScreen()
                    vf = scr.visibleFrame()
                    top = vf.origin.y + vf.size.height
                    icon = None
                    if _anchor_provider is not None:
                        try:
                            icon = _anchor_provider()
                        except Exception:
                            icon = None
                    if icon:
                        icx = icon[0] + icon[2] / 2.0
                    else:
                        icx = vf.origin.x + vf.size.width - 90.0   # repli haut-droite
                    left = icx - self.W / 2.0
                    left = max(vf.origin.x + 6.0,
                               min(left, vf.origin.x + vf.size.width - self.W - 6.0))
                    # v1.0.9 — offset adaptatif (hauteur barre des menus) : même
                    # niveau sous la barre sur 13" et 14" (cf. _top_offset).
                    y = top - self.H + self._top_offset(scr, vf)
                    self.panel.setFrame_display_(NSMakeRect(left, y, self.W, self.H), True)
                    # Bec : position dans la bulle (centrée dans le panel).
                    wrap_left = left + (self.W - self.WRAP_W) / 2.0
                    tail = icx - wrap_left
                    tail = max(18.0, min(tail, self.WRAP_W - 18.0))
                    # v3.2 — icône Dock unique (plus de barre de menus) : sans vraie
                    # ancre, on MASQUE le bec (sinon il pointe dans le vide en
                    # haut-droite). Avec une ancre (si la barre de menus revenait un
                    # jour en option), on le repositionne et on l'affiche.
                    has_anchor = icon is not None
                    js = "ovTail(%s);ovAnchor(%.0f)" % (
                        "true" if has_anchor else "false", tail)
                    if self._ready:
                        self._eval(js)
                    else:
                        # Même borne que js() : la page peut ne jamais charger.
                        if len(self._queue) > 256:
                            self._queue.pop(0)
                        self._queue.append(js)
                except Exception:
                    pass
            _on_main(_run)


# --------------------------------------------------------------------------- #
# API publique (no-op silencieux hors macOS ou si l'overlay n'a pas pu naître)
# --------------------------------------------------------------------------- #
def create():
    """Crée l'overlay sur le main thread (idempotent). Appeler après la fenêtre."""
    global _overlay
    if not _IS_MAC or _overlay is not None:
        return

    def _mk():
        global _overlay
        if _overlay is not None:      # deux create() rapprochés = UN seul panneau
            return
        try:
            _overlay = _Overlay()
            print("[overlay] panneau de dictée prêt (NSPanel non-activant).")
        except Exception as e:
            print(f"[overlay] création KO : {e}")
            return
        # v1.3.4 — état posé avant la création (thème, libellé) : rejoué ici, la
        # page le recevra dès son chargement (file d'attente de _Overlay.js).
        if _theme:
            _js("ovTheme('light')" if _theme == "light" else "ovTheme('dark')")
    _on_main(_mk)


def _js(code):
    if _overlay is not None:
        _overlay.js(code)


def show():
    if _overlay is not None:
        _overlay.show()


def hide():
    """v29.2 — Masquage DUR (alpha fenêtre 0) + reset du contenu. À appeler dès
    que l'overlay NE DOIT PAS être visible : entrée en réunion, fin/abandon/
    figeage de dictée, watchdog anti-fantôme. Garantit zéro chevauchement."""
    if _overlay is not None:
        _js("ovReset()")
        _overlay.hide()


_hotkey_label = None  # libellé du raccourci actif (pied de bulle), ex. "Ctrl + Cmd"


def set_hotkey_label(label):
    """Pousse le libellé RÉEL du raccourci dans le pied de l'overlay (le HTML
    affichait « Ctrl + Cmd » en dur, faux pour 5 raccourcis sur 6)."""
    global _hotkey_label
    _hotkey_label = (label or "").strip() or None
    if _hotkey_label:
        import json as _json
        _js("ovHotkey(%s)" % _json.dumps(_hotkey_label))


_theme = None  # v1.3.4 — thème courant (dark/light), rejoué si l'overlay n'existe pas encore


def set_theme(theme):
    """v1.3.4 — thème de la pilule (dark/light), poussé par app.py au démarrage
    et à chaque changement du réglage « Apparence »."""
    global _theme
    _theme = "light" if theme == "light" else "dark"
    _js("ovTheme('light')" if _theme == "light" else "ovTheme('dark')")


def set_lang(lang):
    """v3.3 — langue de l'overlay de dictée (fr/en), poussée par app.py.set_lang.
    Les libellés internes (Transcription, Texte inséré, Micro requis…) suivent."""
    _js("ovLang('en')" if lang == "en" else "ovLang('fr')")


def recording():
    if _overlay is not None:
        _overlay.reposition()   # ancre la bulle sous l'icône à chaque dictée
    if _hotkey_label:
        import json as _json
        _js("ovHotkey(%s)" % _json.dumps(_hotkey_label))
    show()
    _js("ovRecording()")


def level(v):
    _js("ovLevel(%.4f)" % float(v))


def transcribing(rec_s):
    _js("ovTranscribing(%.3f)" % float(rec_s))


def result(trans_s, inserted, branch=None, text=""):
    import json as _json
    _js("ovResult(%.3f,%s,%s,%s)" % (float(trans_s),
                                     "true" if inserted else "false",
                                     _json.dumps(branch),
                                     _json.dumps((text or "")[:240])))


def too_short():
    show()
    _js("ovTooShort()")


def too_short_tap():
    """v1.3.3 — Appui bref isolé en mode double appui : rien à transcrire, et
    c'est l'occasion d'apprendre le geste (« appuie deux fois »)."""
    show()
    _js("ovTooShortTap()")


def locked(label=None):
    """v1.3.3 — Micro maintenu ouvert après un double appui : la pilule le dit,
    et dit comment terminer (le raccourci réel, pas un libellé en dur)."""
    import json as _json
    _js("ovLocked(%s)" % _json.dumps((label or _hotkey_label or "").strip()))


def error(msg=""):
    import json as _json
    show()
    _js("ovError(%s)" % _json.dumps(msg or ""))


def mic_needed():
    show()
    _js("ovMicNeeded()")


def info(title, sub):
    """Popup d'information générique (ex. permission à accorder)."""
    import json as _json
    show()
    _js("ovInfo(%s,%s)" % (_json.dumps(title or ""), _json.dumps(sub or "")))


def fade_out():
    _js("ovFadeOut()")


def reset():
    _js("ovReset()")
