"""Raccourci global de dictée (maintenir ou appuyer) branché sur le contrôleur.

hotkey_mac.py pose les moniteurs NSEvent et décide de la combinaison ; ce
module y relie la dictée : démarrage, relâchement, overlay, insertion, et
réarmement à chaud quand la touche ou le mode change dans les Réglages.
Les événements ne sont jamais consommés ; aucun rappel ne bloque le thread
principal (le travail part sur des threads démons).
"""
import os
import threading
import time

import applog
import appstate
import errors as _EV
import hotkey_mac
import inserter
import notifier
import overlay
import permissions
from dictation import _insert_serial_lock
from settings import _load_settings
from texts import _te
from ui_bridge import _cancel_overlay_hide, _schedule_overlay_hide, _toast, _ui

log = applog.get("hotkeys")


_hotkey_state = None       # v1.3.3 — état du raccourci global (hotkey_mac.start), pour le réarmer à chaud


def start_global_hotkey():
    """Démarre le raccourci global push-to-talk.

    Mécanisme : moniteurs NSEvent (hotkey_mac — moniteur GLOBAL + moniteur
    local), AUCUNE interception : les événements ne sont JAMAIS consommés.
    Limitation connue : avec ctrl_space et plusieurs sources de saisie
    actives, macOS peut traiter la combo lui-même (bip / bascule de source
    de saisie) — le moniteur n'intercepte rien.

    Permission requise : ACCESSIBILITÉ (pour le moniteur GLOBAL). Sans elle,
    aucun événement clavier ne remonte hors focus : on guide l'utilisateur au
    démarrage (cf. _check_hotkey_perm).

    Raccourci lu depuis les Réglages (clé 'hotkey'), override via env VLOCAL_HOTKEY.
    Valeurs : ctrl_space (défaut) | ctrl_cmd (chord de modificateurs) |
    ctrl_alt_space | ctrl_cmd_space | cmd_shift_x | none.
    """
    global _hotkey_state
    _settings_now = _load_settings()
    hotkey_name = (os.environ.get("VLOCAL_HOTKEY")
                   or _settings_now.get("hotkey")
                   or "ctrl_cmd").lower()   # v20 — même défaut que l'UI/la table
    # v1.3.3 — mode de déclenchement : « hold » (maintenir) ou « tap » (maintenir,
    # ou double appui pour garder le micro ouvert). Indépendant de la touche.
    hotkey_mode = "tap" if _settings_now.get("hotkey_mode") == "tap" else "hold"
    if hotkey_name == "none":
        appstate.hotkey_label = ""   # raccourci désactivé -> pas de libellé (toast/UI)
        log.info("[hotkey] désactivé via VLOCAL_HOTKEY=none — utilisez le bouton Dicter.")
        return None

    # Configs : nom -> (modificateurs requis, vk de la touche déclencheur,
    # libellé, badge symboles pour l'UI). vk = virtual keycode macOS
    # (Espace=49, X=7).
    #
    # CHORD DE MODIFICATEURS (trigger_vk = None) : pas de touche déclencheur, on
    # démarre quand TOUS les modificateurs sont maintenus et on arrête au premier
    # relâché (ex. ctrl_cmd = maintenir Ctrl+Cmd). Aucun bip (les modificateurs
    # seuls ne déclenchent aucune action système) et aucune interception (on ne
    # consomme JAMAIS Ctrl/Cmd, sinon on casserait tout le clavier).
    HOTKEYS = {
        "ctrl_cmd":       ({"ctrl", "cmd"}, None, "Ctrl + Cmd", "⌃ ⌘"),
        "ctrl_space":     ({"ctrl"},        49, "Ctrl + Espace", "⌃ ␣"),
        "ctrl_alt_space": ({"ctrl", "alt"}, 49, "Ctrl + Option + Espace", "⌃ ⌥ ␣"),
        "ctrl_cmd_space": ({"ctrl", "cmd"}, 49, "Ctrl + Cmd + Espace", "⌃ ⌘ ␣"),
        "cmd_shift_x":    ({"cmd", "shift"}, 7, "Cmd + Maj + X", "⌘ ⇧ X"),
        # v1.2.0 — TROIS RACCOURCIS QUI NE MARCHAIENT PAS. Le menu des Réglages
        # proposait « Fn », « Cmd droite » et « Option droite » depuis des mois,
        # mais cette table ne les contenait pas : HOTKEYS.get(...) retombait en
        # SILENCE sur Ctrl + Cmd. L'utilisateur choisissait Fn et voyait Ctrl+Cmd.
        # Chacun est un chord identifié par SA touche (keyCode), pas seulement par
        # son drapeau : le drapeau Function est aussi posé par les flèches et
        # F1-F12, et Cmd droite est indistinguable de Cmd gauche au drapeau seul.
        "fn":             ({"fn"},           None, "Touche Fn", "fn"),
        "right_cmd":      ({"cmd"},          None, "Cmd droite", "⌘"),
        "right_opt":      ({"alt"},          None, "Option droite", "⌥"),
    }
    required, trigger_vk, hotkey_label, hotkey_badge = \
        HOTKEYS.get(hotkey_name, HOTKEYS["ctrl_cmd"])
    # keyCode de la touche EXACTE pour les chords à touche unique (cf. ci-dessus).
    chord_vk = {"fn": hotkey_mac.FN_KEYCODE,
                "right_cmd": hotkey_mac.RIGHT_CMD_KEYCODE,
                "right_opt": hotkey_mac.RIGHT_OPT_KEYCODE}.get(hotkey_name)
    appstate.hotkey_label = hotkey_label
    # v1.3.4 — l'ancien appel setHotkeyLabel() visait une fonction que le tableau
    # de bord n'a plus (il lit le raccourci dans les réglages) : supprimé, comme
    # setEngineReady() ; tests/test_ui_bridge.py garde désormais ce pont.
    # Libellé du raccourci dans le pied de l'overlay (« Relâchez … pour terminer »),
    # faux en dur pour 5 raccourcis sur 6 avant v20.
    try:
        overlay.set_hotkey_label(hotkey_label)
    except Exception:
        log.debug("overlay.set_hotkey_label : échec ignoré", exc_info=True)

    def begin():
        # Nouvelle dictée -> annule un éventuel fade-out de résultat en cours.
        _cancel_overlay_hide()
        # NB : on NE pré-bloque PAS sur le statut micro. Le statut TCC peut être
        # "indéterminé" (binaire fraîchement signé) sans être "refusé" : laisser
        # l'ouverture du flux audio déclencher le PROMPT MICRO NATIF de macOS.
        # Le micro réellement refusé est géré en aval (aucun son -> message clair).
        log.info(f"[hotkey] BEGIN ({hotkey_label})")
        if not appstate.controller:
            # DICT-02 — moteur encore en chargement : feedback au lieu du silence.
            overlay.info(_te("Vlocal démarre"), _te("Le moteur se charge, réessayez dans un instant."))
            _schedule_overlay_hide(2.0)
            return
        # v3.2.8 — micro EXPLICITEMENT refusé (status 2, PAS "indéterminé") : message
        # clair immédiat + on n'enregistre pas du silence (un tap court tombait sinon
        # sur « trop court » et l'utilisateur ne comprenait pas). Le cas "indéterminé"
        # (status 0, binaire fraîchement signé) passe au prompt micro NATIF, inchangé.
        try:
            if permissions.mic_status() == 2:
                overlay.mic_needed()
                _schedule_overlay_hide(5.0)
                return
        except Exception:
            log.debug("permissions.mic_status : échec ignoré", exc_info=True)
        # v1.0.11 — ZÉRO LATENCE PERÇUE : l'overlay s'affiche IMMÉDIATEMENT, AVANT
        # l'ouverture du micro (qui peut prendre un instant au 1er appui, en
        # Bluetooth, ou quand le device bascule). Avant, il n'apparaissait qu'APRÈS
        # le retour de start() -> délai visible à l'appui. La pastille est désormais
        # instantanée ; le micro s'ouvre derrière, et on bascule l'overlay seulement
        # si ça échoue (micro indispo) ou si c'est refusé (occupé).
        overlay.recording()             # overlay État 1 (waveform live) — INSTANTANÉ
        try:
            started = bool(appstate.controller.start())
        except Exception as e:
            # DICT-04 — micro indisponible / périphérique monopolisé : on prévient.
            log.warning(f"[hotkey] micro indisponible : {e}")
            overlay.mic_needed()
            _schedule_overlay_hide(4.0)
            return
        if started:
            # Fiabilité #10 — presse-papier d'origine capturé à l'APPUI (avant que
            # la dictée n'y copie son texte), gardé sur toute une chaîne de dictées
            # et rendu à la fin par inserter (image et fichiers compris).
            try:
                if _load_settings().get("insert_at_cursor", True):
                    inserter.begin_chain()
            except Exception:
                log.warning("[hotkey] capture du presse-papier KO", exc_info=True)
        else:
            # v1.0.22 — le refus « transcription précédente en cours » n'existe
            # plus (dictée enchaînée) : si start() rend False, c'est que le micro
            # n'a pas pu s'ouvrir dans le budget de 2 s. Il est en cours de remise
            # à neuf en fond -> on dit à l'utilisateur la seule chose utile.
            overlay.info(_te("Un instant"), _te("Micro en cours de remise à neuf, réessayez."))
            _schedule_overlay_hide(1.5)

    def end():
        if not appstate.controller:
            return
        if not appstate.controller.recording_active():
            # DICT-01 — anti-race : le worker begin a peut-être posé _active
            # juste après. On re-teste sur une courte fenêtre bornée avant
            # d'abandonner (jamais de dictée fantôme micro-ouvert).
            deadline = time.time() + 0.5
            while time.time() < deadline:
                time.sleep(0.02)
                if appstate.controller.recording_active():
                    break
            else:
                return
        rec_dur = appstate.controller.recording_duration()
        log.info(f"[hotkey] END dur={rec_dur:.2f}s")
        # ÉTAPE 4.4 — capture trop courte : pas d'appel Whisper (hallucinations).
        if rec_dur < 0.3:
            # v1.0.22 — cancel() ferme le micro (join worker 2 s + stop borné) :
            # exécuté ici en synchrone, il RETENAIT LE THREAD PUMP du raccourci,
            # donc retardait l'appui suivant de plusieurs secondes sur un simple
            # tap court. En thread daemon, comme _finalize.
            threading.Thread(target=appstate.controller.cancel, name="tap-cancel",
                             daemon=True).start()
            _ui("dictationCancelled()")
            overlay.too_short()
            _schedule_overlay_hide(2.0)
            return
        # Overlay État 2 (transcription) AVANT le travail bloquant.
        overlay.transcribing(rec_dur)

        # stop_and_finalize() est BLOQUANT (Whisper). On le sort du callback du
        # moniteur NSEvent (sinon il gèle) -> overlay réactif + relâchement non
        # bloqué.
        def _finalize():
            t_rel = time.time()
            my_gen = getattr(appstate.controller, "_gen", 0)   # CONC-5
            # v1.0.22 — chaînage : la VALIDITÉ D'INSERTION ne dépend plus de la
            # génération (une dictée enchaînée qui démarre pendant cette
            # finalisation est désormais NORMALE et ne doit pas faire perdre ce
            # texte) mais de l'epoch, bumpé UNIQUEMENT par force_reset.
            my_epoch = getattr(appstate.controller, "_epoch", 0)

            def _own():
                # True tant que cette dictée reste la plus récente (sinon une
                # nouvelle dictée a démarré -> on ne touche plus l'overlay).
                return my_gen == getattr(appstate.controller, "_gen", my_gen)

            try:
                # notify_ui=False : la fenêtre principale ne réagit pas (overlay seul).
                text = appstate.controller.stop_and_finalize(notify_ui=False)
            except Exception as e:
                log.warning(f"[hotkey]  transcription KO : {e}")
                if _own():
                    overlay.error(_te("Réessayez."))
                    _schedule_overlay_hide(4.0)
                return
            if not text:
                # Silence, panne, dictée perdue... : stop_and_finalize a déjà
                # toasté le détail et donne ici le texte court de SA dictée.
                if _own():
                    overlay.error(_te(appstate.controller.empty_overlay_text()
                                      or "Aucun son capté. Réessayez."))
                    _schedule_overlay_hide(4.0)
                return
            # CONC-5 / v29.2 / v1.0.22 — GARDE-FOU D'INSERTION, réécrit pour le
            # chaînage : une dictée plus récente qui a démarré pendant cette
            # finalisation est désormais le cas NORMAL (l'utilisateur enchaîne)
            # -> on INSÈRE quand même, dans l'ordre FIFO (avant : le texte était
            # jeté au presse-papier = « ça se perd » en usage intensif). Seul un
            # force_reset (epoch bumpé : figeage récupéré par le superviseur)
            # invalide ce texte — c'était le vrai cas visé par l'anti-pollution.
            if getattr(appstate.controller, "_epoch", my_epoch) != my_epoch:
                log.info("[hotkey] finalisation invalidée par un reset -> "
                      "insertion au curseur IGNORÉE (anti-pollution). "
                      "Texte au presse-papier.")
                return
            # INSERTION AU CURSEUR (façon Wispr) ; repli presse-papier garanti.
            # Le raccourci global EST de la dictée (jamais réunion) : on insère
            # toujours, quel que soit le mode affiché dans la fenêtre.
            inserted = False
            _insert_guided = False   # une invite d'échec est-elle affichée via l'overlay ?
            try:
                if _load_settings().get("insert_at_cursor", True):
                    # v1.0.22 — chaînage : collages sérialisés (jamais entrelacés).
                    # Le presse-papier d'origine vient de begin_chain() (INS-1).
                    with _insert_serial_lock:
                        inserted = bool(inserter.insert_at_cursor(text))
                    if not inserted:
                        # v1.0.6 — OBSERVABILITÉ (tour de contrôle) : on compte les
                        # échecs d'insertion, friction n°1 connue (Accessibilité non
                        # accordée). Métadonnée seule (jamais le texte). Permet de
                        # voir combien de clients sont coincés AVANT 100 messages.
                        if _EV:
                            _EV.log(_EV.E.INSERT_FAIL,
                                    acc=bool(permissions.accessibility_ok()))
                        # Le _toast part dans la fenêtre PRINCIPALE, souvent CACHÉE
                        # en mode raccourci -> invisible. On guide donc via l'OVERLAY
                        # (sa propre fenêtre, toujours présente). On VÉRIFIE d'abord
                        # que le texte est réellement au presse-papier (sinon le
                        # message « copié » mentirait).
                        import clipboard as _ck
                        try:
                            copied_ok = (_ck.get_clipboard() == text)
                        except Exception:
                            log.debug("_ck.get_clipboard : échec ignoré", exc_info=True)
                            copied_ok = True
                        # v1.0.22 — chaînage : ne toucher l'overlay QUE si cette
                        # dictée est toujours la plus récente (sinon on écraserait
                        # la wave de la dictée suivante en cours d'enregistrement).
                        if _own():
                            if not copied_ok:
                                overlay.error(_te("Échec d'insertion, réessayez."))
                            elif not permissions.accessibility_ok():
                                overlay.info(_te("Insertion à autoriser"),
                                             _te("Texte copié : Cmd + V. Autorisez Vlocal dans "
                                                 "Réglages > Confidentialité et sécurité > Accessibilité, "
                                                 "puis relancez Vlocal."))
                                try:
                                    permissions.open_settings("accessibility")
                                except Exception:
                                    log.debug("permissions.open_settings : échec ignoré", exc_info=True)
                            else:
                                overlay.info(_te("Texte copié"), _te("Collez avec Cmd + V."))
                            _insert_guided = True
                        _toast("Texte copié dans le presse-papier, collez avec "
                               "Cmd + V. Pour l'insertion automatique au curseur, "
                               "autorisez Vlocal dans Réglages Système > "
                               "Confidentialité et sécurité > Accessibilité.",
                               kind="info")
            except Exception as e:
                log.warning(f"[insert]  insertion au curseur KO : {e}")
            # Overlay État 3 : chrono de transcription + branche cascade + action.
            # Si une invite d'échec est déjà à l'écran (canal fiable), on NE
            # l'écrase PAS avec le résultat -> on prolonge juste l'affichage.
            if _own():
                if _insert_guided:
                    _schedule_overlay_hide(9.0)   # laisser lire l'invite
                else:
                    trans_dur = time.time() - t_rel
                    branch = getattr(appstate.controller.engine, "last_model", None)
                    overlay.result(trans_dur, inserted, branch, text)   # texte fidèle à l'app
                    _schedule_overlay_hide(3.0)
        threading.Thread(target=_finalize, daemon=True).start()

    # FIABILITÉ : begin/end encapsulés -> aucune exception ne se propage au tap.
    def _begin_safe():
        try:
            begin()
        except Exception as e:
            log.warning(f"[hotkey] begin KO (ignoré) : {e}")

    def _end_safe():
        try:
            end()
        except Exception as e:
            log.warning(f"[hotkey] end KO (ignoré) : {e}")

    # v1.3.3 — appui bref isolé en mode double appui : rien à transcrire, on
    # annule et on montre le geste. Même mécanique que « trop court », avec le
    # conseil qui va avec.
    def _cancel_safe():
        try:
            if not appstate.controller:
                return
            threading.Thread(target=appstate.controller.cancel, name="tap-cancel",
                             daemon=True).start()
            _ui("dictationCancelled()")
            overlay.too_short_tap()
            _schedule_overlay_hide(2.6)
        except Exception as e:
            log.warning(f"[hotkey] cancel KO (ignoré) : {e}")

    # v1.3.3 — double appui reçu : le micro reste ouvert, la pilule le dit et
    # dit comment terminer (avec le libellé du raccourci réel).
    def _lock_safe():
        try:
            overlay.locked(hotkey_label)
        except Exception as e:
            log.warning(f"[hotkey] lock KO (ignoré) : {e}")

    # Micro #12 — mains libres : prévenir une minute avant l'arrêt automatique.
    # Notification système (et non l'overlay) : la pilule garde sa waveform.
    # Sans son, pour ne pas l'enregistrer dans la dictée. Thread démon : la
    # notification ne retient jamais le pump du raccourci.
    def _limit_warning_safe():
        def _show():
            try:
                notifier.show(_te("Encore une minute"),
                              _te("La dictée mains libres s'arrêtera dans une minute."),
                              subtitle=None, sound=False)
            except Exception:
                log.warning("[hotkey] avertissement de fin KO", exc_info=True)
        threading.Thread(target=_show, name="hk-limit-warning", daemon=True).start()

    # Raccourci global via moniteurs NSEvent natifs (hotkey_mac), PLUS de
    # pynput : pynput crashait l'app (résolution TSM du caractère hors main
    # thread -> SIGTRAP). Les moniteurs ne lisent que les modificateurs /
    # keycodes -> aucun crash. (hotkey_mac est importé au niveau module, v1.3.2.)
    try:
        log.info(f"[hotkey] '{hotkey_name}' ({hotkey_label}) | "
                 f"accessibility_ok={permissions.accessibility_ok()} "
                 f"mic_ok={permissions.mic_ok()}")
    except Exception:
        # Les sondes de permission passent par pyobjc : un échec ne doit pas
        # empêcher le raccourci de démarrer.
        log.debug("sondes de permission : échec ignoré", exc_info=True)
    # max_seconds = limite des dictées MAINS LIBRES seulement (10 min, annoncée
    # à 9 min) : un micro verrouillé oublié ne reste jamais ouvert. Le maintien
    # n'a pas de limite (Micro #12) : on ne coupe pas quelqu'un qui parle.
    t = hotkey_mac.start(_begin_safe, _end_safe,
                         mods=tuple(required), trigger_vk=trigger_vk,
                         max_seconds=600.0, chord_vk=chord_vk,
                         mode=hotkey_mode, on_cancel=_cancel_safe, on_lock=_lock_safe,
                         on_limit_warning=_limit_warning_safe, warn_before_s=60.0)
    if t is None:
        log.warning("[hotkey] raccourci inactif — accordez l'Accessibilité à Vlocal, "
              "ou utilisez le bouton Dicter.")
    else:
        log.info(f"[hotkey] mode de déclenchement : {hotkey_mode}")
    _hotkey_state = t
    return t


def _rearm_global_hotkey():
    """v1.3.3 — Réarme le raccourci À CHAUD après un changement de touche ou de
    mode dans les Réglages. Avant, le toast disait « Raccourci mis à jour » et
    rien ne changeait avant un redémarrage. Arrêt propre de l'ancien (moniteurs
    retirés, dictée en cours terminée), puis démarrage du nouveau."""
    global _hotkey_state
    old = _hotkey_state
    _hotkey_state = None
    try:
        hotkey_mac.stop(old)
    except Exception as e:
        log.warning(f"[hotkey] arrêt de l'ancien raccourci KO : {e}")
    try:
        t = start_global_hotkey()
        log.info("[hotkey] réarmé à chaud" + ("" if t is not None else " (inactif)"))
    except Exception as e:
        log.exception("[hotkey] réarmement à chaud KO")
        try:
            if _EV:
                _EV.log(_EV.E.HOTKEY_START_FAIL, err=type(e).__name__, rearm=True)
        except Exception:
            log.debug("_EV.log : échec ignoré", exc_info=True)
