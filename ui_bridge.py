"""Pont Python -> interface : tableau de bord, menu V et overlay.

Règle de la boucle d'événements (docs/ARCHITECTURE.md) : rien ici ne bloque le
thread appelant. _ui() pousse du JavaScript sans attendre de réponse ; tout ce
qui touche AppKit est confié à la file principale. _api_safe garantit qu'une
méthode appelée par le JavaScript ne lève jamais vers le pont.
"""
import functools
import json
import sys
import threading

import appstate
import errors as _EV
import overlay
from texts import _te


def _ui(code: str):
    """Exécute du JS dans la fenêtre, NON BLOQUANT (fire-and-forget).

    Bug #1 (hang « ne répond pas ») : NE JAMAIS passer par
    _window.evaluate_js — sur le backend Cocoa il est SYNCHRONE et bloque le
    thread appelant sur un sémaphore SANS timeout jusqu'à ce que le completion
    handler WKWebView se déclenche. Quand le dashboard est masqué (orderOut,
    cas de la dictée au raccourci), la WKWebView ne sert plus ce handler -> le
    worker (boucle audio 10 Hz / finalisation) gèle À VIE, le verrou _busy
    reste tenu et l'app finit par ne plus répondre. On dispatche donc le JS
    directement sur la WKWebView via le main thread, sans attendre (exactement
    comme overlay.py). Aucun appelant de _ui n'utilise sa valeur de retour.
    """
    if appstate.window is None:
        return
    try:
        from webview.platforms.cocoa import BrowserView
        from Foundation import NSOperationQueue
        inst = BrowserView.instances.get(getattr(appstate.window, "uid", "master"))
        web = getattr(inst, "webview", None) if inst is not None else None
        if web is not None:
            NSOperationQueue.mainQueue().addOperationWithBlock_(
                lambda: web.evaluateJavaScript_completionHandler_(code, None))
            return
    except Exception:
        pass
    try:
        # v1.0.22 — repli ultime (accès natif indisponible) rendu NON BLOQUANT :
        # evaluate_js reste synchrone sur un sémaphore SANS timeout (cf. Bug #1
        # ci-dessus) ; tant qu'il était appelé dans le thread courant, ce repli
        # pouvait geler la boucle audio, la finalisation, ou pire le verrou de
        # dictée pendant un _toast. Aucun appelant n'utilise la valeur de retour.
        threading.Thread(target=lambda: appstate.window.evaluate_js(code),
                         name="ui-fallback", daemon=True).start()
    except Exception:
        pass


def _api_safe(default=None):
    """v16.1 — Décorateur : une méthode d'API ne doit JAMAIS lever vers le pont
    JS (sinon la promesse est rejetée et l'UI peut rester bloquée). On capture
    tout, on log côté technique, on renvoie une valeur sûre."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*a, **k):
            try:
                _r = fn(*a, **k)
                # v3.3 — i18n : tout payload {"error": "<FR>"} renvoyé au JS est
                # traduit à la volée selon la langue UI (sinon affiché en FR en mode EN).
                if isinstance(_r, dict) and isinstance(_r.get("error"), str):
                    _r["error"] = _te(_r["error"])
                return _r
            except Exception as e:
                print(f"[api] {fn.__name__} a échoué : {e}")
                # v3.2.9 — tour de contrôle : tout échec d'API journalisé (code
                # stable + méthode + type d'exception, SANS donnée sensible).
                if _EV: _EV.log(_EV.E.API_GUARD, fn=fn.__name__, err=type(e).__name__)
                return default() if callable(default) else default
        return wrapper
    return deco


def _toast(message: str, kind: str = "info"):
    """v15.1 — Affiche un toast clair à l'utilisateur. Helper centralisé :
    une seule façon de parler à l'utilisateur, vouvoiement cohérent.
    kind ∈ {info, success, error} (l'UI peut styliser ; défaut neutre)."""
    try:
        _ui("if(typeof showToast==='function') showToast(%s, %s);"
            % (json.dumps(_te(message or "")), json.dumps(kind)))
    except Exception:
        pass


def _refresh_lists_ui():
    """Pousse la liste des rappels à la fenêtre (silencieux si pas prête)."""
    if appstate.store is None:
        return
    try:
        _ui("renderTasks(%s)" % json.dumps(appstate.store.list_tasks(limit=10)))
    except Exception:
        pass
    # v1.0.6 — STATS LIVE : rafraîchit aussi la vue ouverte (Accueil compris) pour
    # que les compteurs se mettent à jour SANS avoir à quitter/revenir sur l'onglet.
    # No-op sûr si l'UI n'est pas encore prête (garde && côté JS).
    try:
        _ui("window.refreshLists && window.refreshLists()")
    except Exception:
        pass


def _menubar_labels():
    """v3.3 — libellés i18n du menu NSStatusItem (suivent la langue UI backend)."""
    if appstate.ui_lang == "en":
        return {"dictate": "Dictate", "open": "Open Vlocal", "reminders": "Recent reminders",
                "none": "  (none)", "settings": "Settings...", "quit": "Quit Vlocal",
                "update": "Update {0} available..."}
    return {"dictate": "Dicter", "open": "Ouvrir Vlocal", "reminders": "Rappels récents",
            "none": "  (aucune)", "settings": "Réglages...", "quit": "Quitter Vlocal",
            "update": "Mise à jour {0} disponible..."}


def _refresh_menubar():
    """Recharge les items + les libellés i18n du menu bar (best-effort)."""
    if appstate.menubar is None:
        return
    try:
        _tasks = appstate.store.list_tasks(only_open=True, limit=5) if appstate.store else []
        appstate.menubar.refresh(tasks=_tasks, labels=_menubar_labels(), update_version=appstate.update_latest)
    except Exception:
        pass


# v13.2 — Ouverture robuste du modal Réglages.
# Bug terrain v13.1 : clic menu bar "Réglages..." et Cmd+, : rien à l'écran.
# Cause : evaluate_js sur fenêtre cachée = no-op silencieux (pywebview ne
# raise pas), et selon le chemin d'appel l'activation Cocoa pouvait être
# faite hors du main thread.
#
# Solution : un seul point d'entrée robuste, qui :
#   1) Dispatche show() + activateIgnoringOtherApps_ sur main thread Cocoa
#   2) Retente l'injection JS à 3 instants (50/180/420 ms) — la 1ʳᵉ est
#      synchrone (au cas où le DOM est prêt), les 2 autres laissent à
#      WKWebView le temps de rendre la fenêtre visible et le pywebview
#      bridge le temps de se reconnecter
#   3) Loggue chaque étape pour qu'on voie noir sur blanc ce qui se passe
def _open_settings_modal_robust():
    print("[settings] ouverture demandée")

    def _cocoa_show():
        try:
            if appstate.window is not None:
                appstate.window.show()
            from AppKit import NSApplication
            NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        except Exception as e:
            print(f"[settings] show/activate : {e}")

    if sys.platform == "darwin":
        try:
            from Foundation import NSOperationQueue
            NSOperationQueue.mainQueue().addOperationWithBlock_(_cocoa_show)
        except Exception:
            _cocoa_show()
    else:
        try:
            if appstate.window is not None:
                appstate.window.show()
        except Exception:
            pass

    def _inject_later():
        import time as _t
        for delay in (0.05, 0.18, 0.42):
            _t.sleep(delay)
            if appstate.window is None:
                print("[settings] _window None, abandon")
                return
            try:
                # On vérifie que la fonction existe AVANT de l'appeler.
                # evaluate_js synchrone retourne la valeur — None si le JS
                # n'a pas tourné. typeof renvoie 'function' si tout va bien.
                kind = appstate.window.evaluate_js(
                    "typeof openSettingsModal"
                )
                print(f"[settings] tentative t+{delay}s : "
                      f"typeof openSettingsModal = {kind!r}")
                if kind == "function":
                    appstate.window.evaluate_js("openSettingsModal();")
                    print(f"[settings] OK : modal ouvert (t+{delay}s)")
                    return
            except Exception as e:
                print(f"[settings] evaluate_js exception (t+{delay}s) : {e}")
        print("[settings] ÉCHEC FINAL : impossible d'ouvrir le modal "
              "après 3 tentatives.")

    threading.Thread(target=_inject_later, daemon=True).start()


# v2 — L'ancienne 2e fenêtre Réglages (webview) a été supprimée : les Réglages
# sont un modal HTML dans la fenêtre principale (pywebview ne crée pas de 2e
# fenêtre fiable après start()).


# --------------------------------------------------------------------------- #
# Raccourci clavier GLOBAL : Ctrl + Espace, push-to-talk.
# Maintien = enregistre (+ aperçu live) ; relâche = final + copie presse-papier.
# --------------------------------------------------------------------------- #
# v19 — Minuterie de fermeture de l'overlay (résultat -> fade -> orderOut).
# Annulable si une nouvelle dictée démarre pendant l'affichage du résultat.
_overlay_hide_timers = None


def _cancel_overlay_hide():
    global _overlay_hide_timers
    if _overlay_hide_timers:
        for t in _overlay_hide_timers:
            try:
                t.cancel()
            except Exception:
                pass
    _overlay_hide_timers = None


def _schedule_overlay_hide(delay: float):
    """Fade-out à delay-0.3 s, puis masque réellement le panneau à delay s."""
    global _overlay_hide_timers
    _cancel_overlay_hide()

    def _fade():
        overlay.fade_out()

    def _done():
        # v29.2 — masquage DUR (alpha fenêtre 0) en plus du reset de contenu :
        # garantit que l'overlay disparaît même si une trame JS résiduelle/
        # interrompue laissait la carte semi-visible. Pas d'orderOut (le panneau
        # reste prêt ; show() le ré-opacifie).
        overlay.hide()
    t1 = threading.Timer(max(0.0, delay - 0.3), _fade)
    t2 = threading.Timer(max(0.05, delay), _done)
    for t in (t1, t2):
        t.daemon = True
        t.start()
    _overlay_hide_timers = (t1, t2)
