"""API du tableau de bord : réunions.

Méthodes appelées par le JavaScript (window.pywebview.api.*) : démarrer et
arrêter une réunion (micro ou visio), choisir le micro, lister, relire,
réessayer, ré-analyser les locuteurs, renommer, corriger, exporter, gérer les
voix connues. Les transitions passent sous meetings._meeting_lock.
"""
import json
import os
import re
import threading

import applog
import appstate
import errors as _EV
import overlay
import processor
from dictation import _LOOPBACK_KW
from lifecycle import _begin_activity, _end_activity
from meetings import (
    GUIDED_MAX_MIN,
    MAX_AUTO_SPEAKERS,
    _attribute_blocks,
    _meeting_lock,
    _meeting_state,
    _obsidian_meeting_sync,
    _recognize_voices,
    _reunion_transcribe_pipeline,
)
from settings import (
    VISIO_MODE_ENABLED,
    _APP_SUPPORT,
    _diarization_enabled,
    _load_settings,
    _save_settings,
    _voice_memory_enabled,
)
from texts import _te
from ui_bridge import _api_safe, _cancel_overlay_hide, _refresh_lists_ui, _toast, _ui

log = applog.get("api_meetings")


class MeetingsApi:
    """Réunions : enregistrement, relecture, locuteurs, export."""

    # ------------------------ v12 : Mode RÉUNION -----------------------------
    @_api_safe(default=lambda: {"ok": False, "error": "Erreur interne (réunion)."})
    def reunion_start(self, num_speakers=None, mode="presentiel"):
        """v12 — Démarre un enregistrement long en mode Réunion.

        num_speakers (v26, optionnel) : choix du sélecteur « Combien de
        participants ? » pour CETTE réunion (2-6, 0=auto, None=réglage global).
        Stocké dans _meeting_state["n_spk_forced"], consommé une seule fois par
        la pipeline au moment de la diarisation.

        v13.1 — Refuse explicitement de démarrer si :
          (a) une réunion est déjà en enregistrement (`recording=True`)
          (b) la pipeline réunion précédente est encore en cours
              (`pipeline_busy=True`). Sinon les états s'entrechoquaient :
              l'ID dans `_meeting_state` était écrasé par le 2e start
              avant que la 1ʳᵉ pipeline ait fini d'écrire en SQLite, et
              l'UI mélangeait les statuts à l'écran.
        """
        if appstate.controller is None:
            _toast("Le moteur de transcription finit de se préparer, un "
                   "instant…", kind="info")
            return {"ok": False, "error": "Moteur en préparation."}
        if not appstate.reunion_enabled:
            _toast("Le mode Réunion n'est pas activé. Activez-le dans "
                   "Réglages.", kind="info")
            return {"ok": False, "error": "Mode Réunion désactivé."}
        # v29.2 — MUTUELLE EXCLUSION overlay/réunion : on force le masquage DUR de
        # l'overlay de dictée (alpha 0) en entrant en réunion. Sinon un overlay
        # resté visible (dictée figée, transition interrompue) chevauchait l'UI
        # réunion (« deux panneaux superposés »).
        try:
            overlay.hide()
            _cancel_overlay_hide()
        except Exception:
            pass
        # v16.1 — Section critique atomique : check ET prise d'état sous verrou.
        with _meeting_lock:
            if _meeting_state["recording"]:
                return {"ok": False, "error": "Enregistrement déjà en cours."}
            if _meeting_state.get("pipeline_busy"):
                _toast("La réunion précédente est encore en cours de "
                       "traitement. Patientez qu'elle apparaisse comme prête "
                       "dans la liste.", kind="info")
                return {"ok": False,
                        "error": "Pipeline réunion précédente encore en cours."}
            # On marque l'intention TOUT DE SUITE pour bloquer un 2e appel
            # concurrent, avant même d'ouvrir le micro.
            _meeting_state["recording"] = True
            # v1.0.12 — mode de CETTE réunion (présentiel par défaut, visio si demandé).
            _meeting_state["mode"] = mode if mode in ("presentiel", "visio") else "presentiel"
            # v26 — choix per-réunion du sélecteur (None = pas de choix ->
            # purge toute valeur périmée d'une réunion précédente).
            try:
                _meeting_state["n_spk_forced"] = (
                    int(num_speakers) if num_speakers is not None else None)
            except (TypeError, ValueError):
                _meeting_state["n_spk_forced"] = None
            if _meeting_state["n_spk_forced"] is None:
                _meeting_state.pop("n_spk_forced", None)
            try:
                # v13.1 — instance neuve à chaque start (pas de handle wav
                # résiduel d'une session qui aurait planté).
                # v2 — périphérique d'entrée choisi (loopback pour visio, sinon
                # micro par défaut).
                dev = _load_settings().get("audio_device")
                # v1.0.12 — VISIO : recorder qui mixe micro + audio système (helper
                # ScreenCaptureKit) dans le MÊME WAV 16k/mono -> tout l'aval (live,
                # diarisation, pipeline) tourne à l'identique. Repli présentiel
                # automatique si la visio n'est pas disponible (helper absent).
                if _meeting_state.get("mode") == "visio" and self.is_visio_available():
                    from visio_recorder import VisioRecorder
                    rec = VisioRecorder(device=dev)
                else:
                    from recorder import MeetingRecorder
                    rec = MeetingRecorder(device=dev)
                _meeting_state["recorder"] = rec
                wav_path = rec.start()
                import time as _t
                titre = "Réunion " + _t.strftime("%d/%m/%Y %H:%M")
                mid = appstate.store.add_meeting(titre, wav_path, "recording") if appstate.store else None
                _meeting_state["id"] = mid
                # v16 — Transcription INCRÉMENTALE pendant l'enregistrement :
                # au stop, il ne restera qu'une courte fenêtre à traiter (au
                # lieu de passer 1 h de WAV dans Whisper d'un coup).
                _meeting_state["live"] = None
                _meeting_state["diar"] = None
                try:
                    from live_meeting import LiveMeetingTranscriber
                    # WhoTalks v1.1 — diariseur EN LIGNE : on extrait les
                    # empreintes vocales PENDANT la réunion (coût étalé), pour
                    # qu'au stop il ne reste que le clustering (~0,1 s). Chargé
                    # seulement si la diarisation est activée.
                    _diar = None
                    # v1.0.12 — VISIO : PAS de diariseur live CAM++ (la diarisation
                    # se fait par SOURCE micro/système au stop -> on évite de charger
                    # CAM++ + sa fuite d'arène ONNX = RAM ↓, sous le plafond 2,10 Go).
                    if _diarization_enabled() and _meeting_state.get("mode") != "visio":
                        try:
                            import diarizer as _dz
                            if _dz.available():
                                _diar = _dz.Diarizer()
                                _diar.reset_stream()
                        except Exception as e:
                            log.warning(f"[whotalks] diariseur live indispo ({e}).")
                            _diar = None
                    # v1.0.12 — VISIO : fenêtres de transcription plus courtes
                    # (pic RAM MLX ↓ sous 2,10 Go, sans toucher modèle/qualité).
                    # Présentiel : aucun kwarg -> fenêtres engine par défaut (inchangé).
                    _vk = ({"window_target_s": 12.0, "window_max_s": 18.0}
                           if _meeting_state.get("mode") == "visio" else {})
                    lt = LiveMeetingTranscriber(appstate.controller.engine, wav_path,
                                                diarizer=_diar, **_vk)
                    lt.start()
                    _meeting_state["live"] = lt
                    _meeting_state["diar"] = _diar
                except Exception as e:
                    log.warning(f"[live] démarrage incrémental KO ({e}) — "
                          "repli sur transcription complète au stop.")
                # v21 — anti-App Nap pendant l'ENREGISTREMENT : le micro ouvert
                # ne protège PAS d'App Nap (le critère Apple est « playing
                # audio », pas la capture) ; la transcription live tourne
                # fenêtre cachée. Relâchée dans la pipeline (finally) ou sur
                # les chemins d'erreur de reunion_stop.
                _meeting_state["activity"] = _begin_activity("Vlocal meeting recording")
                # v25 — WAVEFORM RÉUNION : sans alimentation de niveau, la wave de
                # l'UI reste FIGÉE à plat pendant une réunion (la boucle
                # setAudioLevel ne tourne qu'en dictée). On pousse ici le RMS du
                # recorder à ~10 Hz, MÊME canal setAudioLevel que la dictée. Thread
                # daemon borné par le drapeau recording (baissé par reunion_stop)
                # -> s'arrête tout seul au stop, aucun thread zombie.
                def _reunion_level_loop(_rec):
                    import time as _lt
                    _last_sys = None
                    while (_meeting_state.get("recording")
                           and getattr(_rec, "recording", False)):
                        try:
                            _ui("setAudioLevel(%.4f)" % _rec.rms_recent())
                            # v1.0.12 — message live si la capture système (visio)
                            # lâche. MeetingRecorder n'a pas system_status -> None
                            # -> ignoré (présentiel strictement inchangé).
                            _ss = getattr(_rec, "system_status", None)
                            if _ss is not None and _ss != _last_sys:
                                _last_sys = _ss
                                if _ss == "denied":
                                    _toast("Audio système non capté : autorisez "
                                           "« Enregistrement de l'écran » pour Vlocal. "
                                           "Réunion en micro seul.", kind="error")
                                elif _ss == "lost":
                                    _toast("Le son système s'est interrompu ; la réunion "
                                           "continue en micro seul.", kind="info")
                                elif _ss == "never":
                                    _toast("Capture du son système indisponible : réunion "
                                           "en micro seul.", kind="info")
                        except Exception:
                            break
                        _lt.sleep(0.1)
                threading.Thread(target=_reunion_level_loop, args=(rec,),
                                 name="vlocal-reunion-level", daemon=True).start()
                overlay.set_meeting(True)   # pilule de repos masquée pendant la réunion
                return {"ok": True, "wav_path": wav_path, "id": mid}
            except Exception as e:
                # Échec micro/disque : on REND l'état (sinon bloqué en
                # "recording" sans flux). Message clair. n_spk_forced purgé
                # (sinon un IMPORT ultérieur hériterait d'un forçage périmé).
                _meeting_state["recording"] = False
                _meeting_state["recorder"] = None
                _meeting_state["id"] = None
                _meeting_state.pop("n_spk_forced", None)
                _meeting_state.pop("mode", None)
                log.warning(f"[reunion] démarrage impossible : {e}")
                _toast("Impossible de démarrer l'enregistrement : "
                       f"{e}. Vérifiez le micro et l'espace disque.",
                       kind="error")
                return {"ok": False, "error": str(e)}

    @_api_safe(default=lambda: {"ok": False, "error": "Erreur interne (réunion)."})
    def reunion_stop(self):
        """v12 — Stop l'enregistrement et lance la transcription en arrière-plan.

        v16.1 — Atomique + idempotent : un double-clic sur « Arrêter » ne
        déclenche qu'UN seul stop / une seule pipeline."""
        with _meeting_lock:
            rec = _meeting_state.get("recorder")
            if not rec or not rec.recording or not _meeting_state["recording"]:
                return {"ok": False, "error": "Aucun enregistrement actif."}
            # On baisse le drapeau TOUT DE SUITE : un 2e appel concurrent verra
            # recording=False et sortira proprement.
            _meeting_state["recording"] = False
            overlay.set_meeting(False)   # la pilule de repos revient
            try:
                wav_path, duree = rec.stop()
            except Exception as e:
                log.warning(f"[reunion] arrêt : {e}")
                _end_activity(_meeting_state.pop("activity", None))  # v21
                return {"ok": False, "error": str(e)}
        # v15 — Remonte un éventuel incident d'enregistrement (disque plein…)
        if getattr(rec, "disk_full", False):
            _ui("if(typeof showToast==='function') showToast(%s);" % json.dumps(
                _te("Espace disque insuffisant : l'enregistrement a été tronqué. "
                    "La partie déjà captée va être transcrite.")))
        elif rec.error:
            log.warning(f"[reunion] incident enregistrement : {rec.error}")
            _toast("Un incident d'écriture a interrompu l'enregistrement ; "
                   "la partie déjà captée va être transcrite.", kind="error")
        # v15 — Audio trop court / silencieux : pas la peine de lancer Whisper.
        if not wav_path or not os.path.exists(wav_path) or duree < 0.6:
            # v16 — arrête proprement le transcripteur incrémental éventuel.
            lt = _meeting_state.get("live")
            _meeting_state["live"] = None
            dz = _meeting_state.get("diar")
            _meeting_state["diar"] = None
            if lt is not None:
                try:
                    lt.finalize()
                except Exception:
                    pass
            # Libère le diariseur live (CAM++) APRÈS lt.finalize() — qui peut
            # encore appeler feed_window et rechargerait l'extracteur si on
            # l'avait déchargé avant. Sans cet unload, l'instance restait
            # référencée (RAM) jusqu'au prochain reunion_start.
            if dz is not None:
                try:
                    dz.unload()
                except Exception:
                    pass
            mid0 = _meeting_state.get("id")
            if appstate.store and mid0:
                try:
                    appstate.store.update_meeting(mid0, duree_audio_s=duree,
                                          status="error")
                except Exception:
                    pass
            _meeting_state.pop("n_spk_forced", None)   # forçage périmé purgé
            _meeting_state.pop("mode", None)
            _toast("Enregistrement trop court, rien à transcrire.", kind="info")
            _refresh_lists_ui()
            _end_activity(_meeting_state.pop("activity", None))  # v21
            return {"ok": False, "error": "Enregistrement trop court."}
        mid = _meeting_state.get("id")
        if appstate.store and mid:
            try:
                appstate.store.update_meeting(
                    mid, duree_audio_s=duree, status="transcribing"
                )
            except Exception:
                pass
        # v16.1 — pipeline_busy posé MAINTENANT (avant de lancer le thread) pour
        # qu'aucun reunion_start ne se faufile dans l'intervalle.
        _meeting_state["pipeline_busy"] = True
        # Lance la pipeline asynchrone : transcription incrémentale
        # (live.finalize(), repli transcribe_detailed) -> diarisation -> SQLite.
        try:
            threading.Thread(
                target=_reunion_transcribe_pipeline,
                args=(mid, wav_path, duree),
                daemon=True,
            ).start()
        except Exception as _err:
            # v3.2.8 — lancement du thread KO (épuisement ressources) : NE JAMAIS
            # laisser pipeline_busy coincé à True, sinon les Réunions sont bloquées
            # à vie ("réunion précédente en cours") jusqu'au redémarrage. On nettoie.
            _meeting_state["pipeline_busy"] = False
            log.warning(f"[reunion] lancement pipeline KO : {_err}")
            if appstate.store and mid:
                try:
                    appstate.store.update_meeting(mid, status="error")
                except Exception:
                    pass
            _toast("Le traitement de la réunion n'a pas pu démarrer. Réessayez.",
                   kind="error")
            _end_activity(_meeting_state.pop("activity", None))
            return {"ok": False, "error": "Le traitement n'a pas pu démarrer."}
        return {"ok": True, "id": mid, "duree": duree}

    # ------------------------ v2 : périphériques audio ----------------------
    @_api_safe(default=list)
    def list_input_devices(self):
        """v2 — Périphériques d'ENTRÉE pour la réunion. Détecte ceux capables de
        capter le son système (loopback) pour transcrire une visio (Meet/Teams)."""
        try:
            import sounddevice as sd
        except Exception:
            return []
        out = []
        for i, d in enumerate(sd.query_devices()):
            if d.get("max_input_channels", 0) > 0:
                name = d.get("name", f"Périphérique {i}")
                is_lb = any(k in name.lower() for k in _LOOPBACK_KW)
                out.append({"index": i, "name": name, "loopback": is_lb})
        return out

    @_api_safe(default=False)
    def set_audio_device(self, index):
        """Mémorise le périphérique d'entrée choisi (None/-1 = défaut système)."""
        try:
            idx = int(index)
        except Exception:
            idx = -1
        # v1.5 — le NOM accompagne l'index : la dictée le fait résoudre par le
        # worker micro, dont l'énumération (à jour) ne suit pas celle d'ici.
        name = None
        if idx >= 0:
            import sounddevice as sd
            name = sd.query_devices(idx).get("name")
        _save_settings({"audio_device": (None if idx < 0 else idx),
                        "audio_device_name": name})
        return True

    @_api_safe(default=False)
    def is_visio_available(self):
        """v1.0.12 — La réunion VISIO est-elle possible ? (helper ScreenCaptureKit
        présent + mode non désactivé). N'importe PAS meeting_visio (donc aucune
        dépendance soundfile sur ce chemin). Utilisé par l'UI (toggle) et par
        reunion_start (repli présentiel automatique)."""
        if not VISIO_MODE_ENABLED:
            return False
        try:
            from visio_recorder import is_helper_available
            return bool(is_helper_available())
        except Exception:
            return False

    @_api_safe(default=list)
    def list_meetings(self):
        return appstate.store.list_meetings(limit=10) if appstate.store else []

    def get_meeting(self, mid):
        """v12e — Retourne le détail complet d'une réunion (transcription
        brute + propre) pour la vue détaillée avec toggle.
        v20 — accès SQL ciblé : O(1) au lieu de charger 50 réunions, et les
        réunions au-delà des 50 dernières redeviennent consultables."""
        if appstate.store is None:
            return None
        try:
            return appstate.store.get_meeting(int(mid))
        except Exception:
            return None

    @_api_safe(default=False)
    def delete_meeting(self, mid):
        if appstate.store:
            appstate.store.delete_meeting(int(mid), delete_wav=True)
            _refresh_lists_ui()
        return True

    @_api_safe(default=False)
    def retry_meeting(self, mid, num_speakers=None):
        """v29.4 — RÉESSAYER une réunion en ÉCHEC : re-traite (transcription +
        diarisation) depuis le WAV sauvegardé, sans ré-enregistrer. Réutilise le
        chemin IMPORT sur le WAV existant et MET À JOUR la réunion en place.
        Récupère notamment les réunions ratées par un ancien build. {ok, id}."""
        import os as _os
        import wave as _wave
        if appstate.controller is None or appstate.store is None:
            return {"ok": False, "error": "Le moteur se prépare encore, un instant."}
        m = appstate.store.get_meeting(mid)
        if not m:
            return {"ok": False, "error": "Réunion introuvable."}
        wav = m["wav_path"] if "wav_path" in m.keys() else None
        if not wav or not _os.path.exists(wav):
            return {"ok": False,
                    "error": "L'audio de cette réunion n'est plus disponible, "
                             "réessai impossible."}
        with _meeting_lock:
            if (_meeting_state.get("recording")
                    or _meeting_state.get("pipeline_busy")):
                return {"ok": False,
                        "error": "Un traitement est déjà en cours, réessayez dans "
                                 "un instant."}
            _meeting_state["pipeline_busy"] = True
        try:
            with _wave.open(wav, "rb") as wf:
                dur = wf.getnframes() / float(wf.getframerate())
            appstate.store.update_meeting(mid, status="transcribing")
            _meeting_state["live"] = None
            _meeting_state["diar"] = None
            _meeting_state["import"] = True          # re-traitement depuis le WAV
            _meeting_state["id"] = mid
            if (num_speakers is not None and str(num_speakers).isdigit()
                    and int(num_speakers) >= 1):
                _meeting_state["n_spk_forced"] = int(num_speakers)
            threading.Thread(target=_reunion_transcribe_pipeline,
                             args=(mid, wav, dur), daemon=True).start()
            return {"ok": True, "id": mid}
        except Exception as e:
            _meeting_state["pipeline_busy"] = False
            log.warning(f"[reunion] retry KO : {e}")
            return {"ok": False, "error": f"Réessai impossible : {e}"}

    def rediarize_meeting(self, mid, num_speakers=0):
        """v18.5 — Relance la diarisation d'une réunion DÉJÀ transcrite, sans
        re-enregistrer : on réutilise l'audio (wav_path) + les segments Whisper
        (confidence_json). Permet de corriger une sur-segmentation en fixant le
        nombre de locuteurs. Renvoie le nb de locuteurs, ou False si impossible."""
        m = self.get_meeting(mid)
        if not m or appstate.store is None:
            return False
        wav = m.get("wav_path")
        if not wav or not os.path.exists(wav):
            _toast("L'audio de cette réunion n'est plus disponible : impossible "
                   "de relancer l'identification des locuteurs.", kind="error")
            return False
        try:
            import diarizer
            if not diarizer.available():
                return False
            segs = []
            try:
                segs = (json.loads(m.get("confidence_json") or "{}")
                        or {}).get("segments", [])
            except Exception:
                segs = []
            try:
                n = int(num_speakers) if str(num_speakers).isdigit() else 0
            except Exception:
                n = 0
            if not n:
                _sp = _load_settings().get("meeting_speakers", "auto")
                n = int(_sp) if str(_sp).isdigit() else 0
            # relais RAM (cf. pipeline) : décharge turbo le temps de re-diariser
            # (rechargé au finally SEULEMENT s'il était chargé — sur MLX, None).
            _rl = False
            try:
                if appstate.controller is not None:
                    _rl = getattr(appstate.controller.engine, "model", None) is not None
                    appstate.controller.engine.unload_model()
            except Exception:
                pass
            dia = diarizer.Diarizer()
            try:
                # v26 — même VOIE PRIMAIRE que la pipeline : guidée-transcription
                # (avec le même plafond RAM), repli tuiles. C'est l'action de
                # RATTRAPAGE de l'utilisateur : elle doit avoir la meilleure
                # qualité disponible, pas l'ancienne (tuiles, DER ~0,35).
                spk = None
                try:
                    _dur = float(m.get("duree_audio_s") or 0)
                except Exception:
                    _dur = 0.0
                if segs and 0 < _dur <= GUIDED_MAX_MIN * 60:
                    try:
                        import wave as _wv
                        with _wv.open(wav, "rb") as _wf:
                            _sr16 = _wf.getframerate()
                        # v27 — extraction en sous-process (RAM plate)
                        # v1.0.24 — AVANCEMENT VISIBLE : sur une réunion d'une
                        # heure, cette étape dure plusieurs minutes ; l'utilisateur
                        # n'avait qu'un toast figé « Ré-identification… » et ne
                        # savait pas si l'app travaillait ou était plantée.
                        def _prog(done, total):
                            pct = int(100 * done / max(1, total))
                            _ui("if(typeof setRediarProgress==='function')"
                                "setRediarProgress(%d);" % pct)
                        spk = dia.diarize_from_segments_subproc(
                            wav, _sr16, segs, num_speakers=n,
                            max_speakers=MAX_AUTO_SPEAKERS, on_progress=_prog)
                        _ui("if(typeof setRediarProgress==='function')"
                            "setRediarProgress(100);")
                    except Exception as e:
                        log.warning(f"[reunion] re-diarisation guidée KO ({e}) -> tuiles.")
                        if _EV: _EV.log(_EV.E.DIAR_FALLBACK, where="rediar_exception",
                                        err=str(e)[:120])
                        spk = None
                if not spk:
                    # v1.0.25 — CE REPLI N'EST PLUS SILENCIEUX. La voie guidée
                    # peut rendre une liste VIDE sans lever (worker d'empreintes
                    # KO) : on basculait alors sur l'ancien chemin par tuiles,
                    # nettement moins bon, sans que rien ne l'indique — d'où des
                    # ré-identifications qui « ne changeaient rien ». On le
                    # journalise et on le dit à l'utilisateur.
                    log.warning("[reunion] voie guidée indisponible -> repli tuiles "
                          "(qualité dégradée).")
                    if _EV: _EV.log(_EV.E.DIAR_FALLBACK, where="rediar_empty")
                    _toast("L'analyse fine des voix n'a pas pu démarrer : le "
                           "résultat est moins précis que d'habitude. Relancez "
                           "la ré-identification, et si cela persiste, "
                           "redémarrez Vlocal.", kind="error")
                    spk = dia.diarize(wav, num_speakers=n,
                                      max_speakers=MAX_AUTO_SPEAKERS)
                voices_map = dia.last_voices()        # v1.2 — centroïdes
                if not spk:
                    return False
                units = diarizer.words_from_segments(segs)
                blocks = _attribute_blocks(dia, units, wav, spk)  # attribution au mot ≥98 %
                gl = appstate.store.glossary_pairs() if appstate.store else []
                if gl:
                    for b in blocks:
                        b["text"] = processor.apply_glossary(b["text"], gl)
                # v1.2 — reconnaissance des voix connues (idem pipeline,
                # silencieuse ici : pas de log).
                auto_names = _recognize_voices(voices_map)
                appstate.store.update_meeting(int(mid), speaker_blocks_json=json.dumps(
                    {"blocks": blocks, "names": auto_names, "voices": voices_map},
                    ensure_ascii=False))
                _obsidian_meeting_sync(mid)
                return len({b["speaker"] for b in blocks})
            finally:
                # Déchargement CAM++ + rechargement turbo GARANTIS, y compris
                # sur le retour anticipé (spk vide) et sur exception — et
                # APRÈS l'attribution au mot (assign_words_precise réutilise
                # l'extracteur : le décharger avant le rechargeait aussitôt).
                try:
                    dia.unload()
                except Exception:
                    pass
                if _rl:
                    try:
                        appstate.controller.engine.load_model()
                    except Exception:
                        pass
        except Exception as e:
            log.warning(f"[reunion] re-diarisation KO : {e}")
            return False

    # ------------------------ v18.2 : panneau réunion à droite ----------------
    @_api_safe(default=False)
    def set_meeting_panel(self, is_open):
        """v3.1 — En dashboard plein écran, le détail réunion est une VUE INTERNE
        du dashboard (panneau dans la fenêtre), plus un redimensionnement natif.
        No-op de sécurité : ne touche plus à la taille de la fenêtre."""
        return True

    # ------------------------ v18 : diarisation (locuteurs) ------------------
    def _load_speaker_payload(self, mid):
        """Charge {blocks, names} d'une réunion depuis speaker_blocks_json."""
        m = self.get_meeting(mid)
        if not m or not m.get("speaker_blocks_json"):
            return None
        try:
            data = json.loads(m["speaker_blocks_json"])
            data.setdefault("blocks", [])
            data.setdefault("names", {})
            return data
        except Exception:
            return None

    # ------------------------------------------------------------------ #
    # v20 (G1) — EXPORT FICHIER d'une réunion. Manque produit identifié par
    # l'audit : toute la matière (blocs locuteurs + horodatages) existait en
    # base sans aucune sortie fichier (seul le presse-papier existait).
    # UN dialogue natif d'enregistrement ; le FORMAT suit l'extension choisie :
    #   .txt = compte rendu lisible (Locuteur : texte)
    #   .md  = idem en Markdown (titres + gras)
    #   .srt = sous-titres horodatés (1 cue par bloc locuteur ; nécessite la
    #          diarisation — sinon repli : un seul cue plein texte si durée connue)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _srt_ts(t):
        t = max(0.0, float(t or 0.0))
        ms_total = int(round(t * 1000.0))
        h, rem = divmod(ms_total // 1000, 3600)
        mn, s = divmod(rem, 60)
        return f"{h:02d}:{mn:02d}:{s:02d},{ms_total % 1000:03d}"

    def _export_content(self, m, data, fmt):
        titre = (m.get("titre") or "Réunion").strip()
        created = m.get("created_at") or ""
        label = lambda spk: ((data or {}).get("names", {}) or {}).get(spk) or \
            ("Voix " + str(int(spk.split("_")[-1]) + 1 if "_" in str(spk) else spk))
        blocks = (data or {}).get("blocks") or []
        if fmt == "srt":
            if not blocks:
                return None
            out = []
            for i, b in enumerate(blocks, 1):
                out.append(f"{i}\n{self._srt_ts(b.get('start'))} --> "
                           f"{self._srt_ts(b.get('end'))}\n"
                           f"{label(b.get('speaker'))} : {b.get('text', '').strip()}\n")
            return "\n".join(out)
        texte = (m.get("transcription_structuree")
                 or m.get("transcription_brute") or "")
        if fmt == "md":
            head = f"# {titre}\n\n_{created}_\n\n"
            if blocks:
                return head + "\n\n".join(
                    f"**{label(b.get('speaker'))}** — {b.get('text', '').strip()}"
                    for b in blocks) + "\n"
            return head + texte + "\n"
        # txt (défaut)
        head = f"{titre}\n{created}\n{'-' * 40}\n\n"
        if blocks:
            return head + "\n\n".join(
                f"{label(b.get('speaker'))} : {b.get('text', '').strip()}"
                for b in blocks) + "\n"
        return head + texte + "\n"

    @_api_safe(default=dict)
    def export_meeting(self, mid):
        """Exporte la réunion vers un fichier choisi par l'utilisateur.
        Le format (.txt / .md / .srt) suit l'extension du nom choisi."""
        m = self.get_meeting(mid)
        if not m:
            return {"ok": False, "error": "Réunion introuvable."}
        data = self._load_speaker_payload(mid)
        safe = re.sub(r"[^\w\s.-]", "", (m.get("titre") or "reunion"),
                      flags=re.UNICODE).strip()[:60] or "reunion"
        try:
            import webview as _wv
            dest = appstate.window.create_file_dialog(
                _wv.SAVE_DIALOG,
                directory=os.path.expanduser("~/Documents"),
                save_filename=safe + ".txt")
        except Exception as e:
            return {"ok": False, "error": f"Dialogue d'enregistrement indisponible : {e}"}
        if not dest:
            return {"ok": False, "cancelled": True}
        path = dest if isinstance(dest, str) else (dest[0] if dest else None)
        if not path:
            return {"ok": False, "cancelled": True}
        fmt = os.path.splitext(path)[1].lower().lstrip(".") or "txt"
        if fmt not in ("txt", "md", "srt"):
            fmt = "txt"
            path += ".txt"
        content = self._export_content(m, data, fmt)
        if content is None:
            return {"ok": False,
                    "error": "Export SRT impossible : cette réunion n'a pas de "
                             "blocs locuteurs (diarisation absente)."}
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
        except Exception as e:
            return {"ok": False, "error": f"Écriture impossible : {e}"}
        return {"ok": True, "path": path, "format": fmt}

    @_api_safe(default=False)
    def rename_speaker(self, mid, speaker_id, name):
        """ÉTAPE 6 — Renomme un locuteur pour cette réunion ET, WhoTalks v1.2,
        ENRÔLE sa voix dans la banque des voix connues (si on l'a entendu assez
        longtemps) : aux réunions suivantes, le nom apparaîtra tout seul. name
        vide -> retire le renommage (la voix connue, elle, reste)."""
        data = self._load_speaker_payload(mid)
        if data is None or appstate.store is None:
            return False
        name = (name or "").strip()
        if name:
            data["names"][speaker_id] = name
        else:
            data["names"].pop(speaker_id, None)
        appstate.store.update_meeting(int(mid), speaker_blocks_json=json.dumps(
            data, ensure_ascii=False))
        _obsidian_meeting_sync(mid)
        # Enrôlement / apprentissage de la voix (centroïde stocké avec la réunion).
        # v29.9 — gaté par le toggle « Mémoriser les voix » (Réglages > Voix
        # connues) : si OFF, on nomme le locuteur dans CETTE réunion mais on
        # n'enregistre AUCUNE empreinte vocale persistante.
        if name and _voice_memory_enabled():
            try:
                import voiceid
                info = (data.get("voices") or {}).get(speaker_id)
                if info and info.get("centroid"):
                    vid = voiceid.enroll_or_update(
                        _APP_SUPPORT, name, info["centroid"],
                        float(info.get("seconds", 0.0)))
                    if vid:
                        log.info(f"[whotalks] voix « {name} » apprise ({vid}, "
                              f"{round(float(info.get('seconds',0)))}s).")
            except Exception as e:
                log.warning(f"[whotalks] enrôlement ignoré ({e}).")
        return True

    # ---------------- WhoTalks v1.2 : banque de voix connues -----------------
    @_api_safe(default=[])
    def list_known_voices(self):
        """Réglages > Voix connues : [{id,name,seconds,meetings}]."""
        import voiceid
        return voiceid.public_list(_APP_SUPPORT)

    @_api_safe(default=False)
    def rename_known_voice(self, voice_id, name):
        import voiceid
        return voiceid.rename(_APP_SUPPORT, voice_id, name)

    @_api_safe(default=False)
    def delete_known_voice(self, voice_id):
        import voiceid
        return voiceid.delete(_APP_SUPPORT, voice_id)

    @_api_safe(default=False)
    def update_meeting_text(self, mid, text):
        """v18.1 — Édition inline de la transcription continue (sans locuteurs).
        Persiste le texte corrigé par l'utilisateur."""
        if appstate.store is None:
            return False
        appstate.store.update_meeting(int(mid), transcription_structuree=(text or ""))
        return True

    @_api_safe(default=False)
    def update_speaker_block_text(self, mid, block_index, text):
        """v18.1 — Édition inline du texte d'un bloc locuteur. Persiste dans
        speaker_blocks_json."""
        data = self._load_speaker_payload(mid)
        if data is None or appstate.store is None:
            return False
        try:
            data["blocks"][int(block_index)]["text"] = (text or "").strip()
        except Exception:
            return False
        appstate.store.update_meeting(int(mid), speaker_blocks_json=json.dumps(
            data, ensure_ascii=False))
        _obsidian_meeting_sync(mid)
        return True
