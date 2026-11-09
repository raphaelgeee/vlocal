"""Fenêtre du tableau de bord et présence de Vlocal dans le Dock (macOS).

La fenêtre pywebview est une vraie fenêtre macOS restylée après affichage :
contenu plein cadre sous les feux, apparence sombre, bande de déplacement
au-dessus de la WKWebView. La fermer la masque (l'app reste vivante pour le
raccourci global). Tout appel AppKit est fait sur le thread principal (file
principale) ; les fonctions de ce module ne bloquent jamais.
"""
import os
import threading

import applog
import appstate
from settings import _APP_SUPPORT

log = applog.get("window_mac")

# Géométrie de la fenêtre principale, point de vérité unique.
WIN_W = 1180            # v3.1 — dashboard plein écran (était 380, carte flottante)
WIN_H = 780             # v3.1 — (était 720)
_DRAG_VIEW_CLS = None   # v3.2.4 — classe NSView « bande de déplacement » (enregistrée une fois)


# v3.2.3 — FIX DÉFINITIF « DEUX ICÔNES DOCK » (constaté sur le vif : 2 process
# "Foreground"). Cause : le multiprocessing.resource_tracker (créé par numba/MLX
# au chargement du moteur) RE-EXÉCUTE le binaire du bundle -> LaunchServices lui
# colle une 2e icône Dock. set_start_method('fork') NE corrige PAS (le tracker se
# ré-exécute toujours — vérifié). SOLUTION détection-free : on démarre TOUTE
# invocation en politique "Prohibited" (aucune icône Dock), puis SEUL le process
# principal passe en "Accessory" (icône barre de menus, AUCUNE icône Dock) APRÈS
# freeze_support. Le child tracker est dérouté PAR freeze_support avant ce point
# -> il reste "Prohibited" (aucune icône). v3.3 : on abandonne "Regular" (icône
# Dock) au profit de l'icône menu-bar V -> 1 SEULE icône, dans la barre des menus
# (pattern Wispr Flow / SuperWhisper), ce qui résout aussi le « deux icônes ».
# v3.3.1 (bug #6) — politique d'activation à 3 états. 0 = Regular (icône DOCK
# + minimisation possible), 1 = Accessory (icône menu-bar V seule, pas de Dock),
# 2 = Prohibited (aucune icône). Le child resource_tracker (numba/MLX) est
# dérouté par freeze_support AVANT tout passage en Regular -> il reste Prohibited
# -> le bug « deux icônes Dock » ne revient pas. Icône Dock seulement pour le
# process principal et quand la fenêtre dashboard est visible.
def _set_dock(policy):
    try:
        from AppKit import NSApplication
        NSApplication.sharedApplication().setActivationPolicy_(policy)
    except Exception:
        pass


# Compat : conserve l'API booléenne existante. True (fenêtre visible) -> Regular
# (icône Dock) ; False (fenêtre masquée) -> Accessory (menu-bar V seul, pas de Dock).
def _set_dock_visible(_visible):
    _set_dock(0 if _visible else 1)


def _purge_webkit_cache_on_update():
    # v3.2.9 — CORRECTIF STALE-UI (trouvé en TEST sur device) : WKWebView met en
    # cache le HTML par URL ; le chemin /Applications étant constant, une MISE À
    # JOUR servait l'ANCIENNE interface (vu : 3.2.9 installée mais design 3.2.8
    # affiché). On vide le cache WebKit UNE SEULE FOIS quand la version change ->
    # chaque MAJ montre la nouvelle UI. Best-effort, jamais bloquant, AVANT toute
    # création de WKWebView (dashboard + overlay).
    try:
        _ui_marker = os.path.join(_APP_SUPPORT, "ui_version.txt")
        try:
            with open(_ui_marker, "r") as _f:
                _last_ui = _f.read().strip()
        except Exception:
            _last_ui = None
        if _last_ui != appstate.app_version:
            import shutil as _sh
            for _wd in ("~/Library/WebKit/com.vlocal.app",
                        "~/Library/Caches/com.vlocal.app"):
                _p = os.path.expanduser(_wd)
                if os.path.isdir(_p):
                    _sh.rmtree(_p, ignore_errors=True)
            try:
                os.makedirs(_APP_SUPPORT, exist_ok=True)
                with open(_ui_marker, "w") as _f:
                    _f.write(appstate.app_version)
            except Exception:
                pass
            log.info(f"[ui]      cache WebKit purgé (MAJ {_last_ui} -> {appstate.app_version}) : UI fraîche garantie.")
    except Exception as _e:
        log.warning(f"[ui]      purge cache WebKit ignorée : {_e}")


def create_main_window(webview, html_path, api):
    """Crée la fenêtre du tableau de bord (appstate.window), dont la fermeture
    masque au lieu de quitter, et pose son style natif."""
    appstate.window = webview.create_window(
        "Vlocal",
        html_path,
        js_api=api,
        width=WIN_W,
        height=WIN_H,
        min_size=(940, 640),   # v3.1 — dashboard redimensionnable
        resizable=True,
        # v3.2.2 — VRAIE fenêtre macOS : feux rouge/jaune/vert FONCTIONNELS,
        # déplaçable, bord net. _style_window_native() (après affichage) rend la
        # barre de titre transparente et étend le contenu sous les feux (look
        # premium type Linear/Things). AVANT (v3.1) : frameless+transparent avec de
        # FAUSSES pastilles non câblées (-> « je ne peux pas quitter ») + un encart
        # sombre de 16px (-> « gros contour »). Repli : si l'astuce premium échoue,
        # fenêtre macOS standard (feux + titre visibles) — jamais bloquée.
        background_color="#05060a",
        on_top=False,
    )
    try:
        appstate.window.events.closing += _on_window_closing
        log.info("[window] fermeture = masquage (agent maintenu en vie).")
    except Exception as _e:
        log.warning(f"[window] hook closing indisponible : {_e}")
    _install_native_style()


# v19 — AGENT DE DICTÉE : fermer la fenêtre ne doit PAS quitter l'app (sinon
# le raccourci global meurt = « ça quitte tout seul »). On CACHE la fenêtre
# et l'app reste vivante en arrière-plan (icône menu bar) ; « Ouvrir Vlocal »
# la ré-affiche. Le raccourci fonctionne donc même fenêtre fermée.
def _on_window_closing():
    # Quitter explicitement (menu bar « Quitter Vlocal ») -> on laisse fermer.
    if appstate.quitting:
        return True
    try:
        appstate.window.hide()
        appstate.window_visible = False
    except Exception:
        pass
    return False   # fermeture fenêtre = masquage -> process maintenu en vie


# v3.2.4 — FENÊTRE PREMIUM, PROPRE & DÉPLAÇABLE : contenu PLEIN CADRE (les vrais
# feux macOS rouge/jaune/vert flottent dans le design, AUCUNE barre visible) +
# une BANDE DE DÉPLACEMENT invisible en haut. Sans barre de titre, la WKWebView
# capte la souris -> la fenêtre n'était plus déplaçable (bug terrain) ; cette vue,
# dont tout glissé déplace la fenêtre (mouseDownCanMoveWindow=YES), corrige ça.
# Appliqué sur le MAIN THREAD à l'affichage (avec retries). Repli : fenêtre macOS
# standard (jamais bloquée).
def _drag_view_cls():
    global _DRAG_VIEW_CLS
    if _DRAG_VIEW_CLS is None:
        import objc
        from AppKit import NSView
        class _VlocalDragView(NSView):
            # v1.0.1 — DRAG ROBUSTE. mouseDownCanMoveWindow=YES seul ne suffisait
            # pas : après le chargement, pywebview refait first responder de la
            # WKWebView, qui re-captait la souris -> figeage ~5s. Ici on DÉCLENCHE
            # explicitement le drag natif de la fenêtre via performWindowDragWithEvent_
            # (API macOS 10.11+, indépendante du first responder et du hit-test-move).
            def mouseDownCanMoveWindow(self):
                return True
            def acceptsFirstMouse_(self, event):
                return True          # attrapable même fenêtre non focus
            def mouseDown_(self, event):
                w = self.window()
                try:
                    if w is not None and w.respondsToSelector_("performWindowDragWithEvent:"):
                        w.performWindowDragWithEvent_(event)
                        return
                except Exception:
                    pass
                try:
                    objc.super(_VlocalDragView, self).mouseDown_(event)
                except Exception:
                    pass
        _DRAG_VIEW_CLS = _VlocalDragView
    return _DRAG_VIEW_CLS
def _style_window_native(_tries=0):
    applied = False
    try:
        from AppKit import NSApplication, NSColor
        from Foundation import NSMakeRect
        dark = NSColor.colorWithSRGBRed_green_blue_alpha_(5/255.0, 6/255.0, 10/255.0, 1.0)
        for w in NSApplication.sharedApplication().windows():
            try:
                # Fenêtre du dashboard = TITRÉE (styleMask & 1) ; l'overlay de
                # dictée est un NSPanel borderless -> exclu.
                if not (w.styleMask() & 1):
                    continue
                w.setTitlebarAppearsTransparent_(True)
                w.setTitleVisibility_(1)                     # NSWindowTitleHidden
                w.setStyleMask_(w.styleMask() | (1 << 15))   # FullSizeContentView -> plein cadre
                w.setBackgroundColor_(dark)                  # tout résidu reste charbon (jamais blanc)
                # v3.2.9 — DÉPLAÇABLE PARTOUT : en plus de la bande de drag native
                # (ci-dessous, agrandie), on autorise le déplacement par le fond.
                try:
                    w.setMovableByWindowBackground_(True)
                except Exception:
                    pass
                # v3.2.6 — APPARENCE SOMBRE forcée : sans ça, le chrome de la barre
                # de titre reste en mode clair (liseré plus clair sous les feux,
                # repéré à l'œil). En dark, la barre devient NOIRE -> feux fondus
                # dans le noir, zéro démarcation (façon Apple).
                try:
                    from AppKit import NSAppearance
                    w.setAppearance_(NSAppearance.appearanceNamed_("NSAppearanceNameDarkAqua"))
                except Exception:
                    pass
                # Bande de déplacement invisible (haut, hauteur d'une barre de
                # titre). Posée UNE seule fois (identifiant). Les feux flottent
                # AU-DESSUS (boutons de fenêtre, z-order supérieur) ; le contenu
                # sous ~28px reste cliquable (les en-têtes ont 30px de marge haute).
                cv = w.contentView()
                if cv is not None:
                    # v3.3.2 — bande de déplacement RE-CRÉÉE à CHAQUE passage (shown ET
                    # loaded). pywebview ré-installe la WKWebView en contentView à la fin
                    # du 1er chargement (~5s, cocoa didFinishNavigation) + lui donne le
                    # focus souris -> l'ancienne bande était enterrée/détachée et la
                    # fenêtre SE FIGEAIT après ~5s. On retire toute ancienne bande puis on
                    # en ajoute une fraîche EN DERNIER -> z-order AU-DESSUS de la webview.
                    for _sv in list(cv.subviews()):
                        try:
                            if (_sv.identifier() or "") == "vlocalDrag":
                                _sv.removeFromSuperview()
                        except Exception:
                            pass
                    # v1.0.8 — FIX CLICS SIDEBAR BLOQUÉS (plein écran surtout) : la bande
                    # de drag est OPAQUE et avale tout clic dans sa zone (mouseDown ->
                    # performWindowDrag). Avant, elle était PLEINE LARGEUR x 64px -> elle
                    # recouvrait le haut de la SIDEBAR (et, en plein écran, le contenu
                    # remonte -> plus d'entrées nav sous la bande), d'où "impossible de
                    # cliquer Historique/onglets". Désormais : la bande EXCLUT la sidebar
                    # (démarre après ses 240px) et fait la hauteur d'une barre de titre.
                    # -> sidebar 100% cliquable ; on drague la fenêtre depuis le header du
                    # contenu (et via -webkit-app-region:drag côté web). Les feux flottent
                    # au-dessus. Le contenu (bouton Dicter ~y155) reste sous la bande.
                    b = cv.bounds()
                    H = 52.0
                    SIDE = 240.0                              # largeur sidebar : JAMAIS couverte
                    dv = _drag_view_cls().alloc().initWithFrame_(
                        NSMakeRect(SIDE, b.size.height - H, max(0.0, b.size.width - SIDE), H))
                    dv.setIdentifier_("vlocalDrag")
                    dv.setAutoresizingMask_(2 | 8)           # WidthSizable | MinYMargin -> ancrée en haut, à droite de la sidebar
                    cv.addSubview_(dv)
                applied = True
            except Exception:
                continue
    except Exception as e:
        log.warning(f"[window] style premium indisponible ({e}) — fenêtre macOS standard.")
        return
    if applied:
        log.info("[window] premium : plein cadre + feux fondus + bande de déplacement.")
        return
    # Fenêtre pas encore prête -> on réessaie quelques tours de boucle.
    if _tries < 15:
        try:
            from Foundation import NSOperationQueue
            NSOperationQueue.mainQueue().addOperationWithBlock_(
                lambda: _style_window_native(_tries + 1))
        except Exception:
            pass


def _on_window_shown():
    try:
        from Foundation import NSOperationQueue
        NSOperationQueue.mainQueue().addOperationWithBlock_(_style_window_native)
    except Exception:
        pass


def _install_native_style():
    """Applique le style natif à l'affichage, après le premier chargement, et
    plusieurs fois pendant les premières secondes."""
    try:
        appstate.window.events.shown += _on_window_shown
    except Exception:
        pass
    # v3.3.2 — RE-APPLIQUER APRÈS CHARGEMENT (fix fenêtre figée ~5s) : pywebview fait
    # setContentView_(webview) + makeFirstResponder_(webview) à la fin de la 1re
    # navigation (cocoa didFinishNavigation), ce qui enterre la bande de drag. On
    # ré-applique style + bande sur « loaded » (postérieur au reset) pour la remettre
    # au-dessus de la webview. addOperationWithBlock_ garantit l'ordre (runloop suivant).
    try:
        appstate.window.events.loaded += _on_window_shown
    except Exception:
        pass
    # v1.0.1 — FILET ANTI-FIGEAGE : pywebview peut ré-installer la contentView à des
    # instants variables après le chargement (gros HTML + polices). On ré-affirme la
    # bande de drag plusieurs fois sur les premières secondes (toujours sur le main
    # thread) pour qu'elle soit TOUJOURS fraîche et au-dessus, quel que soit le timing.
    try:
        from Foundation import NSOperationQueue as _NSOQ
        for _d in (0.6, 1.5, 3.0, 5.0, 7.0, 9.0):
            threading.Timer(_d, lambda: _NSOQ.mainQueue().addOperationWithBlock_(_style_window_native)).start()
    except Exception:
        pass


# v10c — Force la NSWindow vraiment transparente (pywebview ne le fait
# qu'à moitié → rectangle gris résiduel visible). Doit tourner sur le
# main thread Cocoa.
def _force_clear_window():
    # v3.2.9 — DASHBOARD : fenêtre transparente (le contenu CSS .win, ~95%
    # opaque, fait tout le rendu) MAIS avec l'OMBRE macOS RÉTABLIE. Avant :
    # setHasShadow(False) (hérité de l'ère "carte 374px") -> sur le dashboard
    # plein cadre, bord plat "coupé" en haut, disgracieux. La fenêtre étant
    # titrée (coins arrondis natifs), l'ombre épouse le rectangle arrondi =
    # bord premium propre, et le haut s'intègre enfin sans coupure.
    try:
        from AppKit import NSApplication, NSColor
        app = NSApplication.sharedApplication()
        target = None
        for win in app.windows():
            # Fenêtre titrée et large = le dashboard (l'overlay est un NSPanel borderless).
            if (win.styleMask() & 1) and win.frame().size.width > 200:
                target = win
                break
        if target is None:
            for win in app.windows():
                if win.level() == 0 and win.frame().size.width > 100:
                    target = win
                    break
        if target is not None:
            target.setOpaque_(False)
            target.setBackgroundColor_(NSColor.clearColor())
            target.setHasShadow_(True)        # v3.2.9 — ombre premium rétablie
            target.invalidateShadow()
            log.info(f"[window]  ombre premium rétablie ({target.frame().size.width:.0f}x{target.frame().size.height:.0f})")
        else:
            log.warning("[window]  fenêtre dashboard introuvable")
    except Exception as e:
        log.warning(f"[window]  réglage fenêtre impossible : {e}")

