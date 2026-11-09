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

Le verre est natif, posé SOUS la page (qui ne dessine que le contenu) : une
page web transparente ne voit pas les fenêtres derrière elle. Matériau choisi
par choose_material() : NSGlassEffectView (Liquid Glass, macOS 26), sinon
NSVisualEffectView, sinon une forme opaque bleu nuit, imposée aussi par le
réglage « Réduire la transparence ». La page envoie la forme voulue à chaque
changement d'état (canal « vlocal ») ; le verre s'y anime avec la même durée et
la même courbe que le contenu (GLASS_MS, GLASS_CURVE), cadre et rayon calculés
par glass_frame().

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


# Verre : mêmes valeurs que --t et --curve de dictation_overlay.html (test).
GLASS_MS = 260
GLASS_CURVE = (0.2, 0.9, 0.25, 1.0)
PILL_BOTTOM = 14.0     # bas de la pilule dans le panneau (CSS bottom:14px)
GLASS_MARGIN = 8.0     # le verre ne touche jamais le bord : place de l'ombre


def choose_material(glass_available, visual_available, reduce_transparency):
    """Matériau du verre : "glass" (Liquid Glass), "visual" (NSVisualEffectView)
    ou "opaque". « Réduire la transparence » impose l'opaque : c'est le réglage
    d'accessibilité, il passe avant l'esthétique."""
    if reduce_transparency:
        return "opaque"
    if glass_available:
        return "glass"
    if visual_available:
        return "visual"
    return "opaque"


def glass_frame(geom, panel=(PANEL_W, PANEL_H), bottom=PILL_BOTTOM, margin=GLASS_MARGIN):
    """Cadre (x, y, w, h) et rayon du verre dans le panneau (repère AppKit,
    origine en bas à gauche) pour la forme envoyée par la page :
    geom = {"w", "h", "r"} en px CSS. Centré, posé à `bottom` du bas, borné au
    panneau ; le rayon ne dépasse jamais la demi-hauteur (capsule)."""
    pw, ph = panel
    w = max(0.0, min(float(geom["w"]), pw - 2 * margin))
    h = max(0.0, min(float(geom["h"]), ph - bottom - margin))
    r = max(0.0, min(float(geom["r"]), h / 2.0))
    return ((pw - w) / 2.0, bottom, w, h, r)


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
    from AppKit import (
        NSView, NSVisualEffectView, NSAppearance, NSWorkspace, NSAnimationContext,
        NSVisualEffectMaterialHUDWindow, NSVisualEffectMaterialPopover,
        NSVisualEffectBlendingModeBehindWindow, NSVisualEffectStateActive,
        NSViewWidthSizable, NSViewHeightSizable,
    )
    from Quartz import CAMediaTimingFunction, CABasicAnimation, CAGradientLayer
    import WebKit

    def _glass_class():
        """NSGlassEffectView si le système l'a (macOS 26), sinon None."""
        try:
            return objc.lookUpClass("NSGlassEffectView")
        except objc.nosuchclass_error:
            return None

    def _reduce_motion():
        return bool(NSWorkspace.sharedWorkspace().accessibilityDisplayShouldReduceMotion())

    def _reduce_transparency():
        return bool(NSWorkspace.sharedWorkspace().accessibilityDisplayShouldReduceTransparency())

    def _rgba(rgb, a):
        return NSColor.colorWithSRGBRed_green_blue_alpha_(rgb[0] / 255.0, rgb[1] / 255.0, rgb[2] / 255.0, a)

    _NIGHT = (0x11, 0x14, 0x1F)     # --vl-night
    _WHITE = (0xFF, 0xFF, 0xFF)

    class _Glass:
        """Le verre sous la page. `view` est la vue dont on anime le cadre ;
        `rounded` les calques dont on anime le rayon."""

        def __init__(self, material, theme, frame, radius):
            self.material = material
            self.theme = theme
            light = theme == "light"
            x, y, w, h = frame
            rect = NSMakeRect(x, y, w, h)
            self.rounded = []
            if material == "glass":
                v = _glass_class().alloc().initWithFrame_(rect)
                v.setCornerRadius_(radius)
                # Teinte : garde le texte lisible sur un fond blanc comme noir
                # (même dosage que la simulation CSS, contrastes vérifiés).
                v.setTintColor_(_rgba(_WHITE, 0.70) if light else _rgba(_NIGHT, 0.72))
                self.view = v
                return
            # Visual effect ou opaque : un conteneur porte l'ombre ; dedans, le
            # fond (arrondi, masqué) et un liseré spéculaire éclairé par le haut.
            outer = NSView.alloc().initWithFrame_(rect)
            outer.setWantsLayer_(True)
            sh = outer.layer()
            sh.setShadowColor_(NSColor.blackColor().CGColor())
            sh.setShadowOpacity_(0.22 if light else 0.38)
            sh.setShadowRadius_(14.0)
            sh.setShadowOffset_((0.0, -6.0))
            inner_rect = NSMakeRect(0, 0, w, h)
            if material == "visual":
                fill = NSVisualEffectView.alloc().initWithFrame_(inner_rect)
                fill.setMaterial_(NSVisualEffectMaterialPopover if light else NSVisualEffectMaterialHUDWindow)
                fill.setBlendingMode_(NSVisualEffectBlendingModeBehindWindow)
                fill.setState_(NSVisualEffectStateActive)
                fill.setAppearance_(NSAppearance.appearanceNamed_("NSAppearanceNameAqua" if light
                                                                  else "NSAppearanceNameDarkAqua"))
                fill.setWantsLayer_(True)
            else:
                fill = NSView.alloc().initWithFrame_(inner_rect)
                fill.setWantsLayer_(True)
                fill.layer().setBackgroundColor_((_rgba(_WHITE, 1.0) if light else _rgba(_NIGHT, 1.0)).CGColor())
            rim = NSView.alloc().initWithFrame_(inner_rect)
            rim.setWantsLayer_(True)
            rl = rim.layer()
            rl.setBorderWidth_(0.5)
            rl.setBorderColor_((_rgba(_NIGHT, 0.10) if light else _rgba(_WHITE, 0.16)).CGColor())
            sheen = CAGradientLayer.layer()
            sheen.setColors_([_rgba(_WHITE, 0.30 if light else 0.14).CGColor(), _rgba(_WHITE, 0.0).CGColor()])
            # AppKit compte y vers le haut : 1 = haut du calque.
            sheen.setStartPoint_((0.5, 1.0))
            sheen.setEndPoint_((0.5, 0.55))
            sheen.setFrame_(rl.bounds())
            sheen.setAutoresizingMask_(2 | 16)          # kCALayerWidthSizable | HeightSizable
            rl.addSublayer_(sheen)
            for sub in (fill, rim):
                sub.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
                sub.layer().setCornerRadius_(radius)
                sub.layer().setMasksToBounds_(True)
                outer.addSubview_(sub)
                self.rounded.append(sub.layer())
            self.view = outer

        def animate(self, frame, radius, duration):
            x, y, w, h = frame
            timing = CAMediaTimingFunction.functionWithControlPoints____(*GLASS_CURVE)
            view, rounded = self.view, self.rounded

            def group(ctx):
                ctx.setDuration_(duration)
                ctx.setTimingFunction_(timing)
                ctx.setAllowsImplicitAnimation_(True)
                view.animator().setFrame_(NSMakeRect(x, y, w, h))
                if self.material == "glass":
                    view.animator().setCornerRadius_(radius)
            NSAnimationContext.runAnimationGroup_completionHandler_(group, None)
            # Les calques d'une vue n'animent pas leur rayon implicitement :
            # animation explicite, même durée, même courbe.
            for layer in rounded:
                if duration > 0:
                    anim = CABasicAnimation.animationWithKeyPath_("cornerRadius")
                    anim.setFromValue_(layer.presentationLayer().cornerRadius()
                                       if layer.presentationLayer() is not None else layer.cornerRadius())
                    anim.setToValue_(radius)
                    anim.setDuration_(duration)
                    anim.setTimingFunction_(timing)
                    layer.addAnimation_forKey_(anim, "cornerRadius")
                layer.setCornerRadius_(radius)

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

    class _GeomHandler(NSObject, protocols=[objc.protocolNamed("WKScriptMessageHandler")]):
        """Reçoit la forme voulue par la page (canal « vlocal »)."""

        def initWithOwner_(self, owner):
            self = objc.super(_GeomHandler, self).init()
            if self is None:
                return None
            self._owner = owner      # réf forte, même cycle borné que _NavDelegate
            return self

        def userContentController_didReceiveScriptMessage_(self, ucc, message):
            body = message.body()
            try:
                geom = {k: float(body[k]) for k in ("w", "h", "r")}
            except (KeyError, TypeError, ValueError) as e:
                print(f"[overlay] forme illisible ignorée : {e}")
                return
            self._owner._on_geometry(geom)

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
            self._geom_handler = _GeomHandler.alloc().initWithOwner_(self)   # réf forte
            cfg.userContentController().addScriptMessageHandler_name_(self._geom_handler, "vlocal")
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
            # Conteneur : le verre dessous, la page (contenu seul) dessus.
            content = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, w, h))
            content.setWantsLayer_(True)
            web.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
            content.addSubview_(web)
            panel.setContentView_(content)
            self._content = content
            self.panel = panel
            self.web = web
            self._geom = {"w": 36.0, "h": 22.0, "r": 11.0}       # repos (GEOM.idle de la page)
            self._theme = _theme or "dark"
            self._glass = None
            self._install_glass()
            if os.path.exists(html_path):
                url = NSURL.fileURLWithPath_(html_path)
                base = NSURL.fileURLWithPath_(os.path.dirname(html_path))
                web.loadFileURL_allowingReadAccessToURL_(url, base)
            else:
                print(f"[overlay] HTML introuvable : {html_path}")
            # Panneau TOUJOURS présent (la pilule de repos y vit) : jamais
            # d'orderOut, qui rendait le ré-affichage capricieux.
            panel.orderFrontRegardless()

        def _install_glass(self):
            """(Re)pose le verre selon le matériau disponible et le thème ; sans
            effet si rien n'a changé. Appelé à la création, à chaque appui (le
            réglage « Réduire la transparence » peut avoir changé) et au
            changement de thème."""
            # NSVisualEffectView existe sur tout macOS pris en charge (11 et plus).
            material = choose_material(_glass_class() is not None, True, _reduce_transparency())
            if (self._glass is not None and self._glass.material == material
                    and self._glass.theme == self._theme):
                return
            x, y, w, h, r = glass_frame(self._geom)
            glass = _Glass(material, self._theme, (x, y, w, h), r)
            if self._glass is not None:
                self._glass.view.removeFromSuperview()
            self._content.addSubview_positioned_relativeTo_(glass.view, -1, self.web)  # NSWindowBelow
            self._glass = glass
            self.js("ovGlass('native')")

        def _on_geometry(self, geom):
            self._geom = geom
            x, y, w, h, r = glass_frame(geom)
            self._glass.animate((x, y, w, h), r, 0.0 if _reduce_motion() else GLASS_MS / 1000.0)

        def set_theme(self, theme):
            def _run():
                self._theme = theme
                self._install_glass()
            _on_main(_run)

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
                    self._install_glass()
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
    if _overlay is not None:
        _overlay.set_theme(_theme)


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
