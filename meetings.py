"""Réunions : de l'enregistrement au compte rendu par locuteur.

À l'arrêt d'une réunion (ou à l'import d'un fichier, ou au réessai), le
pipeline finalise la transcription (live_meeting.py a déjà traité l'essentiel
pendant l'enregistrement), sépare les locuteurs (diarizer.py, en
sous-processus), reconnaît les voix connues (voiceid.py), attribue les mots
aux blocs, enregistre la réunion et met à jour la note Obsidian.
_meeting_state décrit la réunion en cours ; _meeting_lock rend ses transitions
atomiques (pywebview peut servir deux appels JS en parallèle).
"""
import json
import os
import threading
import time

import appstate
import errors as _EV
import notifier
import obsidian
import processor
from engine import SAMPLE_RATE
from lifecycle import _begin_activity, _end_activity
from settings import _APP_SUPPORT, _diarization_enabled, _load_settings, _save_settings
from ui_bridge import _refresh_lists_ui, _toast, _ui


# v12 — état du mode Réunion (recorder + meeting id en cours)
_meeting_state = {
    "recorder": None,
    "recording": False,
    "id": None,
    "pipeline_busy": False,   # v13.1 — pipeline réunion en cours sur la précédente
    "live": None,             # v16 — transcripteur incrémental en cours
    "mode": "presentiel",     # v1.0.12 — 'presentiel' | 'visio'
}
# v16.1 — Verrou RÉENTRANT pour rendre ATOMIQUES les transitions réunion.
# pywebview peut exécuter deux appels JS->Python en parallèle (threads
# distincts) : sans ce verrou, un double-clic sur « Démarrer » pouvait passer
# deux fois le test `recording` et créer DEUX enregistrements/flux micro.
_meeting_lock = threading.RLock()


def _obsidian_meeting_sync(meeting_id):
    """v1.1.0 — Écrit (ou met à jour) la note Obsidian d'une réunion si un coffre
    est connecté. Appelé à la fin du pipeline, après une ré-analyse et après un
    renommage de locuteur. Thread daemon, échec silencieux : la réunion est déjà
    en base quoi qu'il arrive."""
    try:
        _s = _load_settings()
        if not (_s.get("obsidian_enabled") and _s.get("obsidian_vault")) or appstate.store is None:
            return
        vault = _s.get("obsidian_vault")
        def _run():
            try:
                m = appstate.store.get_meeting(int(meeting_id))
                if not m:
                    return
                blocks, names = None, {}
                try:
                    payload = json.loads(m.get("speaker_blocks_json") or "null")
                    if isinstance(payload, dict):
                        blocks = payload.get("blocks") or None
                        names = payload.get("names") or {}
                except Exception:
                    pass
                obsidian.capture_meeting(m, vault, blocks=blocks, names=names)
            except Exception as e:
                print(f"[reunion] note Obsidian ignorée ({e}).")
        threading.Thread(target=_run, name="obsidian-meeting", daemon=True).start()
    except Exception:
        pass


# v18.5 — Plafond AUTO de locuteurs côté app (3 sites : frise live, repli WAV,
# re-diarisation). 6 en auto ; l'UI permet de FORCER jusqu'à 8 et le forçage
# bypasse ce plafond. NB : les défauts du moteur divergent entre eux
# (diarizer.diarize / finalize_stream = 8, _diarize_offline = 6) — à arbitrer
# en Phase 2.
MAX_AUTO_SPEAKERS = 6
# v27 — plafond de durée de la diarisation GUIDÉE-TRANSCRIPTION (voie primaire).
# L'extraction tourne en SOUS-PROCESS par lots (diarize_from_segments_subproc) :
# la RAM du process principal reste PLATE (~100-200 Mo, l'arène ONNX meurt avec
# chaque worker). Le plafond n'est plus qu'une borne de bon sens (temps
# d'extraction ~0,2-0,6 s/min d'audio). Mesuré : 34 min -> frise+attribution en
# ~20 s, RSS principal 207 Mo.
GUIDED_MAX_MIN = 240
# v3.4.0 — GARDE ANTI-OOM du repli diarisation IN-PROCESS : ce chemin (dia.diarize /
# tuiles) lit tout le WAV en RAM + ~1 inférence CAM++/s (fuite arène ONNX ~3 Go à
# 34 min, ~10 Go+ à 2h). Il ne sert QUE si la voie sous-process échoue (spawn KO) ou
# si la réunion dépasse GUIDED_MAX_MIN. Au-delà de ce plafond (aligné sur le cap live
# de 40 min) on DÉGRADE en « sans diarisation » plutôt que risquer un OOM sur 8 Go.
DIAR_INPROC_MAX_MIN = 40
# v29.3 — RÉUNION LIVE : au stop, on RE-TRANSCRIT proprement le WAV complet
# (segments Whisper nets) au lieu de diariser sur la transcription incrémentale
# (fenêtres ~25 s qui fusionnent des tours -> interjections courtes avalées).
# Mesuré sur réunion live réelle : 97,6 % (incrémental) -> 98,9 % (re-transcrit).
# Réservé aux réunions <= ce seuil (les très longues gardent l'incrémental : une
# re-transcription de 2 h au stop serait trop longue/chaude). +temps assumé pour
# la précision (choix utilisateur : « précision ultime »).
LIVE_RETRANSCRIBE_MAX_MIN = 45


def _recognize_voices(voices_map, log=False):
    """WhoTalks v1.2 — RECONNAISSANCE : compare le centroïde de chaque voix aux
    voix connues ; si match confiant, le nom apparaît tout seul. Conservateur
    (seuil + marge) : en cas de doute on laisse « Locuteur N ». Partagé par la
    pipeline réunion et rediarize_meeting. log=True : trace les voix reconnues
    et les échecs (chemin pipeline uniquement, comme avant factorisation)."""
    auto_names = {}
    try:
        import voiceid
        known = voiceid.load(_APP_SUPPORT)
        if known and voices_map:
            # v29 — ASSIGNATION GLOBALE par confiance décroissante. L'ancienne
            # boucle en ordre de dict + exclude_ids pouvait « voler » la bonne
            # voix au mauvais cluster -> noms INVERSÉS entre deux participants. On
            # collecte d'abord les matchs CONFIANTS (seuil + marge de
            # voiceid.match), puis on attribue la paire la PLUS sûre en premier,
            # en retirant cluster ET voix utilisés. Un cluster sans match
            # confiant reste « Locuteur N » (fail-safe, jamais un nom faux).
            cands = []
            for spk, info in voices_map.items():
                v, sc = voiceid.match(info.get("centroid"), known)
                if v:
                    cands.append((float(sc), spk, v["id"], v["name"]))
            cands.sort(reverse=True, key=lambda t: t[0])
            used_v = set()
            for sc, spk, vid, name in cands:
                if spk in auto_names or vid in used_v:
                    continue
                auto_names[spk] = name
                used_v.add(vid)
            if auto_names and log:
                print(f"[whotalks] voix reconnues : {auto_names}")
    except Exception as e:
        if log:
            print(f"[whotalks] reconnaissance ignorée ({e}).")
    return auto_names


def _attribute_blocks(diar_obj, units, wav_path, spk_segs):
    """Attribution au MOT (≥98 % mesuré sur réunions réelles) : empreinte
    serrée par mot -> centroïde le plus proche (capte les interruptions /
    chevauchements). Replis successifs : labels de fenêtre (assign_words),
    puis fusion par segments (offline). Partagé pipeline / rediarize."""
    import diarizer
    blocks = None
    # v1.0.23 — VOIE PRIMAIRE : attribution par UNITÉ DE PAROLE. L'ancien étage
    # décidait mot par mot sur des empreintes de 0,4 s — sous le plancher où une
    # empreinte vocale identifie encore quelqu'un (DER x2,7 entre 1,5 s et 0,5 s,
    # Park et al. ICASSP 2021) -> 15,4 changements de locuteur/min et 56 % de
    # tours coupés en pleine phrase sur une réunion réelle. On réutilise la frise
    # (déjà ~3,4 s d'audio par empreinte) au lieu de la jeter : zéro inférence en
    # plus, zéro RAM en plus, et plus rapide. VLOCAL_DIAR_UNITS=0 restaure
    # intégralement l'ancien comportement.
    # ⚠️ DÉSACTIVÉ PAR DÉFAUT (13/08). Mesuré sur la réunion réelle de 69 min
    # contre un diariseur indépendant (segmentation neuronale pyannote + autre
    # modèle d'empreintes) : l'attribution par unité AMÉLIORE la pureté
    # (88,9 % contre 84,4 % — elle tient mieux un seul locuteur dans un vrai
    # tour) mais DÉGRADE la couverture (80,5 % contre 87,6 % — elle fusionne
    # des tours de locuteurs différents ; vu à l'œil : deux personnes qui se
    # saluent réunies en un seul bloc). F1 84,5 contre 86,0 : ce n'est PAS un
    # progrès net, donc on ne l'impose pas aux clients sur une intuition.
    # VLOCAL_DIAR_UNITS=1 pour l'activer et poursuivre l'évaluation.
    if diar_obj is not None and os.environ.get("VLOCAL_DIAR_UNITS", "0") == "1":
        try:
            blocks = diar_obj.assign_units(units)
        except Exception as e:
            print(f"[whotalks] assign_units KO ({e}) -> repli attribution au mot.")
            blocks = None
    if diar_obj is not None and not blocks:
        try:
            # v27 — empreintes-mot extraites en SOUS-PROCESS (l'arène ONNX à
            # tailles variables meurt avec le worker ; blocs bit-identiques à
            # l'in-process, vérifié). Si le spawn échoue, les mots incertains
            # retombent sur leur LABEL-FENÊTRE (~98 % mesuré) — pas d'extraction
            # in-process ici (c'est voulu : RAM bornée avant tout).
            _fn = None
            try:
                import wave as _wv
                with _wv.open(wav_path, "rb") as _wf:
                    _fn = diarizer.make_subproc_embedder(
                        wav_path, _wf.getframerate(), mode="raw")
            except Exception:
                _fn = None
            blocks = diar_obj.assign_words_precise(units, wav_path,
                                                   embed_batch_fn=_fn)
        except Exception as e:
            print(f"[whotalks] assign_words_precise KO ({e}).")
        if not blocks:
            try:
                blocks = diar_obj.assign_words(units)
            except Exception as e:
                print(f"[whotalks] assign_words KO ({e}).")
    if not blocks:
        blocks = diarizer.merge_transcript_with_speakers(units, spk_segs)
    # v28 — filet anti-boucles (« c'est c'est ... » x60) sur le texte AFFICHÉ
    # des blocs (construit depuis les MOTS, donc pas couvert par le nettoyage
    # des segments). Réunion uniquement (_attribute_blocks n'est jamais dictée).
    try:
        from engine import collapse_repetition_loops as _crl
        for b in blocks or []:
            b["text"] = _crl(b.get("text") or "")
    except Exception:
        pass
    return blocks


def _visio_relabel_blocks(blocks, dom_path):
    """v1.0.12 — VISIO : réétiquette les blocs par SOURCE plutôt que de diariser
    le mix (la diarisation du mix donne des frontières fausses / labels qui
    swappent en plein milieu — CHECK 3). Le sidecar .dom contient (mic_rms,
    sys_rms) par bloc de 50 ms : micro dominant -> « Toi », système dominant ->
    « Interlocuteur ». On fusionne ensuite les blocs voisins du même locuteur
    (recolle les phrases coupées). Réservé à la visio (sidecar présent).
    Best-effort : renvoie les blocs inchangés si quoi que ce soit échoue.
    NB v1 : optimisé 1:1 (toi vs interlocuteur). Le multi-locuteurs (séparer les
    différents participants système) reste à brancher (diarisation système-seul)."""
    import numpy as _np
    try:
        dom = _np.fromfile(dom_path, dtype="<f4")
        if dom.size < 4:
            return blocks
        dom = dom.reshape(-1, 2)               # [N,2] : (mic_rms, sys_rms) / bloc 50 ms
    except Exception:
        return blocks
    _BS = 0.05
    relabeled = []
    for b in (blocks or []):
        try:
            s = max(0, int(float(b.get("start", 0.0)) / _BS))
            e = max(s + 1, int(float(b.get("end", 0.0)) / _BS))
            seg = dom[s:e]
            mic_e = float(_np.mean(seg[:, 0])) if seg.size else 0.0
            sys_e = float(_np.mean(seg[:, 1])) if seg.size else 0.0
        except Exception:
            mic_e = sys_e = 0.0
        nb = dict(b)
        # Ids au format SPEAKER_NN (l'UI les reconnaît : couleur stable + renommage).
        # Le nom affiché « Toi »/« Interlocuteur » est fourni via `names` (cf. pipeline).
        nb["speaker"] = "SPEAKER_00" if mic_e > sys_e else "SPEAKER_01"
        relabeled.append(nb)
    # Fusion des blocs voisins du même locuteur (recolle les phrases coupées).
    merged = []
    for nb in relabeled:
        if merged and merged[-1].get("speaker") == nb.get("speaker"):
            merged[-1]["end"] = nb.get("end", merged[-1].get("end"))
            t0 = (merged[-1].get("text") or "").rstrip()
            t1 = (nb.get("text") or "").lstrip()
            merged[-1]["text"] = (t0 + " " + t1).strip()
        else:
            merged.append(dict(nb))
    return merged


# v12 — Pipeline Réunion : transcription incrémentale (live.finalize() /
# transcribe_incremental), repli transcribe_detailed -> diarisation -> SQLite.
def _reunion_transcribe_pipeline(meeting_id, wav_path, audio_duree):
    """Asynchrone. Notifie l'UI à chaque étape via _ui.

    v13.1 — Marque `_meeting_state["pipeline_busy"]` à True pendant toute la
    durée, à False (try/finally) à la fin succès OU échec. Sans ce verrou,
    un 2e reunion_start pendant une pipeline qui dure 30-60 s sur un long
    enregistrement écrasait `_meeting_state["id"]` et l'UI mélangeait les
    statuts.
    """
    _meeting_state["pipeline_busy"] = True
    # v21 — anti-App Nap aussi pendant la pipeline réunion (fenêtre cachée +
    # micro fermé = throttling possible en pleine transcription/diarisation).
    _act = _begin_activity("Vlocal meeting pipeline")
    try:
        return _reunion_transcribe_pipeline_inner(meeting_id, wav_path, audio_duree)
    except Exception as e:
        # v22.2 — GARDE-FOU ULTIME : une exception non prévue (ce thread n'a
        # personne au-dessus pour la rattraper) ne doit JAMAIS laisser le timer
        # « Transcription en cours » tourner à l'infini. On la convertit en
        # erreur propre côté UI.
        import traceback as _tb
        print(f"[reunion] pipeline EXCEPTION : {e}\n{_tb.format_exc()}")
        try:
            _meeting_fail(meeting_id,
                          "La transcription de la réunion a échoué. L'audio est "
                          "conservé, vous pouvez réessayer.")
        except Exception:
            pass
    finally:
        _meeting_state["pipeline_busy"] = False
        _end_activity(_act)
        # relâche aussi l'assertion posée pendant l'ENREGISTREMENT (reunion_start)
        _end_activity(_meeting_state.pop("activity", None))


def _meeting_fail(meeting_id, message, extra_fields=None):
    """v22.2 — Sortie d'erreur TERMINALE du pipeline réunion. CORRIGE LE BUG DU
    « timer infini » : chaque chemin d'échec doit ARRÊTER le compteur UI
    (setMeetingStage 'error'), sinon « Transcription en cours » tournait sans
    fin (l'utilisateur ne voyait jamais la réunion aboutir ni échouer). On
    marque la réunion en erreur (récupérable), on prévient, on rafraîchit."""
    try:
        if appstate.store and meeting_id:
            appstate.store.update_meeting(meeting_id, status="error", **(extra_fields or {}))
    except Exception as e:
        print(f"[reunion] update_meeting(error) KO : {e}")
    if _EV: _EV.log(_EV.E.MEETING_FAIL, mid=str(meeting_id))
    if message:
        _toast(message, kind="error")
    _ui("setMeetingStage(%s)" % json.dumps("error"))   # ARRÊTE le timer UI
    _refresh_lists_ui()


def _reunion_transcribe_pipeline_inner(meeting_id, wav_path, audio_duree):
    import time as _t
    if not wav_path or not os.path.exists(wav_path):
        _meeting_fail(meeting_id,
                      "Le fichier de la réunion est introuvable. "
                      "L'enregistrement n'a pas pu être retrouvé sur le disque.")
        return

    # Étape 1 : transcription. v16 — On privilégie la transcription INCRÉMENTALE
    # déjà faite pendant l'enregistrement (live.finalize() ne traite que la
    # courte fenêtre finale). Repli sur la transcription complète du fichier si
    # le live est absent / a échoué / n'a rien produit.
    _ui("setMeetingStage(%s)" % json.dumps("transcribing"))
    t0 = _t.perf_counter()
    detailed = None
    live = _meeting_state.get("live")
    wt_diar = _meeting_state.get("diar")     # WhoTalks : empreintes accumulées
    is_import = bool(_meeting_state.get("import"))
    _meeting_state["live"] = None
    _meeting_state["diar"] = None
    _meeting_state["import"] = False
    # IMPORT d'un fichier : transcription INCRÉMENTALE par fenêtres + PACING
    # (charge CPU lissée -> pas de chauffe) au lieu du monobloc transcribe_detailed,
    # ET alimentation du diariseur EN LIGNE -> diarisation quasi instantanée ensuite.
    if is_import:
        imp_diar = None
        try:
            import diarizer as _dz
            import wave as _wave
            import numpy as _np
            # Durée/format via l'EN-TÊTE seulement (aucun chargement audio).
            with _wave.open(wav_path, "rb") as _wf:
                _sr = _wf.getframerate(); _ch = _wf.getnchannels()
                _nfr = _wf.getnframes()
            _dur_min = _nfr / float(_sr * 60.0)
            _econ = _load_settings().get("cpu_economy", False)
            # v22.3 — POLITIQUE RAM/THERMIQUE mesurée (pic RSS sur 12,6 min,
            # turbo seul = 1760 Mo) :
            #   batched 4 : 3536 Mo (+1776), 71 % CPU -> swap sur 8 Go +
            #               ventilateurs MÊME sur 24 Go. ET 0 gain de temps vs
            #               batch 2 quand c'est pacé (le pacing domine). À BANNIR.
            #   batched 2 : 2672 Mo (+912), pour les fichiers COURTS sur machine
            #               LARGE (rafale brève, vrai gain de vitesse à pace 0).
            #   incrémental : 1760 Mo (turbo seul), 64 % CPU = LE PLUS FRAIS et
            #               RAM-minimal, +15 % de temps seulement. C'est le choix
            #               par défaut pour 8 Go ET pour tout fichier long.
            # Le batched n'est donc retenu QUE : machine > 8 Go ET fichier court
            # (<= 5 min) ET pas en mode silencieux. Sinon -> incrémental.
            # v23 — si le GPU MLX est dispo, on N'utilise PAS le batched
            # (spécifique CPU/faster-whisper) : l'incrémental route vers
            # transcribe_window -> MLX par fenêtre (~7x, bref) et nourrit le
            # diariseur en ligne. Le batched reste pour les machines CPU-only.
            import mlx_engine as _mlx
            _total_gb = _total_ram_gb()
            _use_batched = ((not _econ) and not _mlx.available()
                            and _total_gb > 8.0 and _dur_min <= 5.0)
            # v27 — la diarisation des IMPORTS passe par la voie GUIDÉE en fin
            # de pipeline (sous-process, RAM plate, DER 0,039) : nourrir les
            # TUILES pendant la transcription est devenu redondant (temps + RAM
            # arène pour un simple repli). On ne les nourrit plus QUE pour les
            # fichiers au-delà du plafond guidé (replis historiques).
            _feed_tiles = _dur_min > GUIDED_MAX_MIN
            if _use_batched:
                _bs = 2   # JAMAIS 4 (gâchis RAM/thermique pour 0 gain mesuré)
                if _feed_tiles and _diarization_enabled() and _dz.available():
                    imp_diar = _dz.Diarizer(); imp_diar.reset_stream()
                # fichier court (<= 5 min) : rafale brève -> peu/pas de pacing.
                _pace_b = 0.0 if _dur_min <= 3 else 0.45
                detailed = appstate.controller.engine.transcribe_import(
                    wav_path, batch_size=_bs, pace=_pace_b, mode="reunion",
                    on_progress=lambda p: _ui("setMeetingProgress(%.2f)" % p),
                    on_window=((lambda pcm, s, off: imp_diar.feed_window(pcm, s, off))
                               if imp_diar is not None else None))
                if detailed and detailed.get("text"):
                    wt_diar = imp_diar
                    print(f"[import] batched x{_bs} pace={_pace_b} "
                          f"({len(detailed.get('segments', []))} segments, "
                          f"RAM totale {_total_gb:.0f} Go).")
                else:
                    detailed = None
                    if imp_diar is not None and wt_diar is not imp_diar:
                        try:
                            imp_diar.unload()
                        except Exception:
                            pass
                        imp_diar = None
            if detailed is None:
                # VOIE LISSE (mode silencieux, RAM serrée, ou échec batched) :
                # incrémental par fenêtres + PACING + diarisation EN LIGNE
                # (tuiles seulement au-delà du plafond guidé, cf. _feed_tiles).
                if _feed_tiles and _diarization_enabled() and _dz.available():
                    imp_diar = _dz.Diarizer(); imp_diar.reset_stream()
                # v20 (RAM) — np.memmap int16 du WAV : ~0 RAM audio côté app
                # (avant : tout le fichier en float32, ~460 Mo pour 2 h). Le
                # moteur convertit PAR FENÊTRE (byte-identique). Offset 44 sûr :
                # WAV écrit par import_meeting_audio (wave, en-tête standard).
                _au = _np.memmap(wav_path, dtype="<i2", mode="r", offset=44)
                _au = _dz._downmix(_au, _ch)
                # v27 — PLEIN DÉBIT GPU pour l'import : gros blocs ~10 min vers
                # mlx_whisper (fenêtrage 30 s natif) = ×28 temps réel mots
                # inclus, vs ~×13 en fenêtres 25 s. Seulement quand les tuiles
                # ne sont pas nourries (la diarisation guidée en sous-process
                # prend le relais en fin de pipeline). Repli incrémental sinon.
                # <= 45 min : enveloppe RAM mesurée/verrouillée du plein débit.
                # Au-delà : fenêtré incrémental (RAM stable éprouvée, ~×13).
                if _mlx.available() and not _feed_tiles and _dur_min <= 45:
                    try:
                        detailed = appstate.controller.engine.transcribe_import_mlx(
                            _au, sr=_sr,
                            on_progress=lambda p: _ui("setMeetingProgress(%.2f)" % p))
                        print(f"[import] plein débit GPU "
                              f"({len(detailed.get('segments', []))} segments).")
                    except Exception as e:
                        print(f"[import] plein débit GPU KO ({e}) -> incrémental.")
                        detailed = None
                if detailed is None:
                    # PACING ADAPTATIF v22.4 : MESURÉ sur fichier réel de 8,7 min,
                    # pace=0 donnait un duty de 0,99 SOUTENU ~2 min -> ventilateurs.
                    # Seuls les fichiers très courts (<= 3 min, rafale brève sous
                    # l'inertie thermique) passent sans pause ; au-delà : PLANCHER
                    # 0,3 puis rampe avec la durée. Le pacing n'affecte que le
                    # rythme, jamais le texte.
                    if _mlx.available():
                        # GPU : rafales brèves par fenêtre, pas de chauffe
                        # soutenue -> aucun pacing (toute la vitesse).
                        _pace = 0.0
                    elif _dur_min <= 3:
                        _pace = 0.3 if _econ else 0.0
                    else:
                        _ramp = max(0.3, min(0.8, (_dur_min - 12) / 45.0 + 0.3))
                        _pace = max(_ramp, 0.6) if _econ else _ramp
                    detailed = appstate.controller.engine.transcribe_incremental(
                        _au, sr=_sr,
                        on_progress=lambda p: _ui("setMeetingProgress(%.2f)" % p),
                        on_window=((lambda pcm, s, off: imp_diar.feed_window(pcm, s, off))
                                   if imp_diar is not None else None),
                        pace=_pace)
                    print(f"[import] transcription incrémentale + diarisation en "
                          f"ligne ({len(detailed.get('segments', []))} segments, "
                          f"pacing={_pace}).")
                wt_diar = imp_diar
        except Exception as e:
            print(f"[import] incrémental KO ({e}) -> repli monobloc.")
            detailed = None
            # Libère CAM++ si l'incrémental a échoué AVANT la passation à
            # wt_diar : sinon l'extracteur resterait chargé pendant tout le
            # repli monobloc (turbo + CAM++ simultanés sur 8 Go). La garde
            # « wt_diar is not imp_diar » préserve les empreintes si l'échec
            # survient APRÈS la passation.
            if imp_diar is not None and wt_diar is not imp_diar:
                try:
                    imp_diar.unload()
                except Exception:
                    pass
                imp_diar = None
    if live is not None and not getattr(live, "failed", False):
        try:
            detailed = live.finalize()
            if detailed and detailed.get("text"):
                print(f"[reunion] transcription incrémentale OK "
                      f"({len(detailed.get('segments', []))} segments).")
            else:
                detailed = None
        except Exception as e:
            print(f"[reunion] finalize live KO : {e} — repli complet.")
            detailed = None
    elif live is not None:
        try:
            live.finalize()   # arrête proprement la boucle
        except Exception:
            pass
    # v29.3 — QUALITÉ DIARISATION live : remplace les segments incrémentaux
    # (grossiers) par une RE-TRANSCRIPTION propre du WAV complet pour une réunion
    # LIVE COURTE -> diarisation guidée sur segments Whisper nets (interjections
    # courtes plus avalées). Le texte affiché en direct pendant l'enregistrement
    # n'est pas touché ; on ne re-transcrit qu'AU STOP, et seulement pour les
    # courtes. Échec -> on garde l'incrémental (sans régression).
    if not is_import and detailed is not None:
        try:
            import wave as _wvq
            import numpy as _npq
            import mlx_engine as _mlxq
            # FIX v29.3.1 : durée calculée LOCALEMENT depuis le WAV (l'ancien code
            # référençait _dur_min, défini SEULEMENT dans la branche import ->
            # UnboundLocalError en live -> réunion en erreur).
            with _wvq.open(wav_path, "rb") as _wfq:
                _srq = _wfq.getframerate()
                _durq_min = _wfq.getnframes() / float(_srq * 60.0)
            if _mlxq.available() and _durq_min <= LIVE_RETRANSCRIBE_MAX_MIN:
                _auq = _npq.memmap(wav_path, dtype="<i2", mode="r", offset=44)
                _clean = appstate.controller.engine.transcribe_import_mlx(
                    _auq, sr=_srq,
                    on_progress=lambda p: _ui("setMeetingProgress(%.2f)" % p))
                if _clean and _clean.get("segments"):
                    detailed = _clean
                    print("[reunion] live court -> re-transcription propre pour "
                          f"diarisation ({len(_clean.get('segments', []))} seg).")
            elif (not _mlxq.available()) and _durq_min <= LIVE_RETRANSCRIBE_MAX_MIN:
                # v1.0.1 — REPLI CPU (sans GPU MLX) : les segments live incrémentaux
                # sont trop grossiers (~18 s) -> le diariseur est affamé et s'effondre
                # à 1 SEUL locuteur (« catastrophique »). On re-transcrit proprement au
                # CPU (transcribe_detailed = segments mot-à-mot fins) pour nourrir la
                # diarisation. Restaure la qualité v29.3 sur machine sans GPU (clients
                # macOS < 14 / Intel + bascule CPU transitoire). Aucun effet si MLX actif.
                _cleancpu = appstate.controller.engine.transcribe_detailed(
                    wav_path, mode="reunion",
                    on_progress=lambda p: _ui("setMeetingProgress(%.2f)" % p))
                if _cleancpu and _cleancpu.get("segments"):
                    detailed = _cleancpu
                    print("[reunion] live court (repli CPU) -> re-transcription propre "
                          f"pour diarisation ({len(_cleancpu.get('segments', []))} seg).")
        except Exception as e:
            print(f"[reunion] re-transcription propre KO ({e}) -> incrémental gardé.")
    if detailed is None:
        # v27 — repli ROUTÉ GPU d'abord : transcribe_detailed chargerait le
        # turbo CT2 (~1,7 Go) même sur machine MLX (contrat RAM violé). On
        # tente le plein débit GPU (memmap, RAM bornée) avant le monobloc CPU.
        try:
            import mlx_engine as _mlxf
            if _mlxf.available():
                import wave as _wvf
                import numpy as _npf
                with _wvf.open(wav_path, "rb") as _wff:
                    _srf = _wff.getframerate()
                _auf = _npf.memmap(wav_path, dtype="<i2", mode="r", offset=44)
                detailed = appstate.controller.engine.transcribe_import_mlx(
                    _auf, sr=_srf,
                    on_progress=lambda p: _ui("setMeetingProgress(%.2f)" % p))
                print("[reunion] repli plein débit GPU OK.")
        except Exception as e:
            print(f"[reunion] repli GPU KO ({e}) -> monobloc CPU.")
            detailed = None
    if detailed is None:
        try:
            detailed = appstate.controller.engine.transcribe_detailed(
                wav_path, mode="reunion",
                on_progress=lambda p: _ui("setMeetingProgress(%.2f)" % p),
            )
        except Exception as e:
            print(f"[reunion] transcription échouée : {e}")
            _meeting_fail(meeting_id,
                          "La transcription de la réunion n'a pas pu aboutir. "
                          "L'enregistrement audio est conservé, réessayez.")
            return
    dur_transcribe = _t.perf_counter() - t0
    raw_text = detailed.get("text", "")
    segments = detailed.get("segments", [])
    avg_conf = detailed.get("avg_conf", 1.0)
    if not raw_text:
        _meeting_fail(meeting_id,
                      "Aucune parole n'a été détectée dans cet enregistrement. "
                      "Vérifiez que le micro captait bien le son.",
                      extra_fields={"duree_transcription_s": dur_transcribe})
        return

    glossary = appstate.store.glossary_pairs() if appstate.store else []
    # Brut = transcription Whisper + glossaire (sécurité noms propres).
    brut = processor.apply_glossary(raw_text, glossary) if glossary else raw_text

    if appstate.store and meeting_id:
        appstate.store.update_meeting(
            meeting_id,
            transcription_brute=brut,
            duree_transcription_s=dur_transcribe,
            status="formatting",
        )

    # Étape 2 : v15 — MISE EN FORME PAR RÈGLES (déterministe, fidèle) :
    # paragraphage par pauses (segments), ponctuation, tics, puis glossaire.
    # Plus aucune IA générative.
    _ui("setMeetingStage(%s)" % json.dumps("structuring"))
    try:
        structured = processor.format_by_rules(raw_text, segments=segments)
    except Exception as e:
        print(f"[reunion] mise en forme KO : {e} — brut conservé.")
        structured = brut
    if glossary:
        structured = processor.apply_glossary(structured, glossary)

    # v15 — Données de confidence pour l'affichage (mots peu fiables en gris).
    conf_json = None
    try:
        conf_json = json.dumps({"segments": segments, "avg_conf": avg_conf},
                               ensure_ascii=False)
    except Exception:
        conf_json = None

    # v18 — DIARISATION (qui parle quand), EN RELAIS, optionnelle, repli
    # silencieux. Backend LÉGER sherpa-onnx (~0,35 Go) : turbo + diarisation =
    # ~2,05 Go < 2,5 Go, donc on NE décharge PAS Whisper (le rechargement
    # coûterait plus que ce qu'on économise). Garde-fous : si désactivé dans les
    # Réglages, si sherpa/modèles absents, ou si la diarisation échoue -> on
    # stocke speaker_blocks=None et l'UI affiche le texte continu comme avant.
    speaker_blocks_json = None
    _visio_dom = (wav_path or "") + ".dom"
    if os.path.exists(_visio_dom):
        # v1.0.12 — VISIO : diarisation par SOURCE (dominance micro/système),
        # CAM++ NON chargé (RAM ↓, sous 2,10 Go). « Toi » = micro, « Interlocuteur »
        # = système. Présentiel (pas de sidecar .dom) garde CAM++ -> branche elif.
        _ui("setMeetingStage(%s)" % json.dumps("diarizing"))
        try:
            _vb = _visio_relabel_blocks(segments, _visio_dom)
            if glossary:
                for _b in _vb:
                    _b["text"] = processor.apply_glossary(_b["text"], glossary)
            if _vb:
                speaker_blocks_json = json.dumps(
                    {"blocks": _vb,
                     "names": {"SPEAKER_00": "Toi", "SPEAKER_01": "Interlocuteur"},
                     "voices": {}}, ensure_ascii=False)
                print(f"[visio] diarisation par source (sans CAM++) -> {len(_vb)} blocs")
        except Exception as _e:
            print(f"[visio] diarisation source KO ({_e}) -> texte continu.")
        _meeting_state.pop("n_spk_forced", None)   # purge (sinon contamine la réunion suivante)
    elif _diarization_enabled():
        try:
            import diarizer
            if diarizer.available():
                _ui("setMeetingStage(%s)" % json.dumps("diarizing"))
                td0 = _t.perf_counter()
                # v18.5 — nombre de locuteurs : "auto" (défaut) ou un entier fixé
                # par l'utilisateur (Réglages > Mode Réunion). Le forcer est la
                # voie la plus FIABLE (ex. 2 pour un appel à deux).
                try:
                    _sp = _load_settings().get("meeting_speakers", "auto")
                    n_spk_set = int(_sp) if str(_sp).isdigit() else 0
                except Exception:
                    n_spk_set = 0
                # v26 — choix PER-RÉUNION du sélecteur « Combien de participants ? »
                # (posé par reunion_start, consommé ICI une seule fois). Prioritaire
                # sur le réglage global ; les imports/rediarisations, qui ne passent
                # pas par le sélecteur, gardent le réglage des Réglages (pas de
                # contamination d'un choix ponctuel).
                _forced = _meeting_state.pop("n_spk_forced", None)
                if _forced is not None:
                    n_spk_set = int(_forced)
                # WhoTalks v1.1 — VOIE RAPIDE : si le diariseur live a accumulé
                # les empreintes PENDANT la réunion, il ne reste que le clustering
                # (~0,1 s). Aucun relais RAM, aucun re-scan du WAV, zéro
                # ralentissement au stop (fin du beachball par construction).
                spk_segs = None
                voices_map = {}          # WhoTalks v1.2 — centroïdes par voix
                diar_obj = None          # diariseur ayant produit spk_segs (labels fenêtre)
                dia = None               # diariseur de REPLI (offline)
                _reloaded = False        # turbo déchargé le temps du repli
                # Fix « CAM++ chargé deux fois par réunion » : la libération de
                # CAM++ et le rechargement de turbo sont DIFFÉRÉS après
                # l'attribution au mot (assign_words_precise réutilise
                # l'extracteur ; le décharger avant le rechargeait aussitôt).
                # Le finally élargi garantit la libération sur TOUTES les
                # sorties (succès, frise vide, exception).
                try:
                    # v26 — VOIE PRIMAIRE : diarisation GUIDÉE PAR LA TRANSCRIPTION
                    # (DER 0,039 vs 0,35 en tuiles, + fusion-liens anti « voix
                    # inventée par l'intonation »). Les tuiles (live puis offline)
                    # deviennent des REPLIS. RAM : on RÉUTILISE le diariseur live
                    # s'il existe (même extracteur CAM++, jamais deux instances) ;
                    # turbo déchargé pendant l'extraction (relais, rechargé au
                    # finally). En cas d'échec/frise vide, l'état stream du live
                    # est INTACT -> replis inchangés.
                    # PLAFOND RAM (revue v26) : l'extraction CAM++ in-process fuit
                    # ~6-12 Mo/inférence (arène ONNX, non corrigeable in-process).
                    # Enveloppe VALIDÉE : 34 min ~ 3 Go de pic. Au-delà de
                    # GUIDED_MAX_MIN on garde les replis historiques (pas de
                    # régression RAM sur réunion longue) en attendant le fix
                    # complet (extraction en sous-process par lots, prototypé).
                    if segments and (audio_duree or 0) <= GUIDED_MAX_MIN * 60:
                        # Relais RAM : ne recharger turbo au finally QUE s'il
                        # était réellement chargé (sur MLX, model est None par
                        # design -> sinon on chargerait 1,7 Go de CT2 pour rien).
                        try:
                            _reloaded = getattr(appstate.controller.engine, "model", None) is not None
                            appstate.controller.engine.unload_model()
                        except Exception:
                            pass
                        # v27 — relais GPU : la transcription est finie, on rend
                        # la RAM MLX (~0,8-1,5 Go) pendant la diarisation. Rechargé
                        # paresseusement au prochain usage (dictée/réunion), comme
                        # l'idle-unloader. Jamais pendant une dictée en cours.
                        try:
                            import mlx_engine as _mlxe
                            if _mlxe.available() and not getattr(appstate.controller, "_busy", False):
                                _mlxe.unload()
                        except Exception:
                            pass
                        _dg = None
                        _tg0 = _t.perf_counter()
                        try:
                            _dg = wt_diar if wt_diar is not None else diarizer.Diarizer()
                            try:
                                import wave as _wv
                                with _wv.open(wav_path, "rb") as _wf:
                                    _sr16 = _wf.getframerate()
                            except Exception:
                                _sr16 = SAMPLE_RATE
                            # v27 — extraction en SOUS-PROCESS (RAM principale
                            # plate ; repli in-process automatique si spawn KO).
                            spk_segs = _dg.diarize_from_segments_subproc(
                                wav_path, _sr16, segments,
                                num_speakers=n_spk_set,
                                max_speakers=MAX_AUTO_SPEAKERS)
                            if spk_segs:
                                voices_map = _dg.last_voices()
                                diar_obj = _dg
                                if _dg is not wt_diar:
                                    dia = _dg   # libéré au finally (après l'attribution)
                                print(f"[whotalks] frise GUIDÉE-TRANSCRIPTION : "
                                      f"{len(spk_segs)} segments en "
                                      f"{_t.perf_counter()-_tg0:.2f}s.")
                            else:
                                if _dg is not wt_diar:
                                    dia = _dg
                                print("[whotalks] guidé-transcription vide -> repli tuiles.")
                        except Exception as e:
                            # même transfert que la branche frise-vide : pas
                            # d'extracteur orphelin si une étape future lève.
                            if _dg is not None and _dg is not wt_diar and dia is None:
                                dia = _dg
                            print(f"[reunion] diarisation guidée KO ({e}) -> repli tuiles.")
                            spk_segs = None
                    elif segments:
                        print(f"[whotalks] réunion > {GUIDED_MAX_MIN} min -> "
                              "diarisation tuiles (plafond RAM, fix sous-process à venir).")
                    if not spk_segs and wt_diar is not None and wt_diar.stream_count >= 3:
                        n_emb = wt_diar.stream_count
                        _tl0 = _t.perf_counter()   # chrono LOCAL (td0 inclurait la tentative guidée)
                        try:
                            spk_segs = wt_diar.finalize_stream(
                                num_speakers=n_spk_set,
                                max_speakers=MAX_AUTO_SPEAKERS)
                            voices_map = wt_diar.last_voices()
                            diar_obj = wt_diar
                            print(f"[whotalks] frise EN LIGNE : {n_emb} empreintes "
                                  f"-> {len(spk_segs)} segments en "
                                  f"{_t.perf_counter()-_tl0:.2f}s.")
                        except Exception as e:
                            print(f"[whotalks] finalize_stream KO ({e}) -> repli.")
                            spk_segs = None
                        if not spk_segs:
                            # Échec / frise vide : on libère CAM++ live TOUT DE
                            # SUITE, AVANT le repli qui charge son propre
                            # extracteur (sinon deux CAM++ coexisteraient
                            # pendant dia.diarize). En cas de succès, la
                            # libération est différée au finally (après
                            # l'attribution au mot).
                            diar_obj = None
                            try:
                                wt_diar.unload()
                            except Exception:
                                pass
                    # REPLI (live absent / échoué) : diarisation complète du WAV
                    # avec relais RAM (décharge turbo le temps de scanner avec
                    # CAM++ ; turbo est rechargé dans le finally, après
                    # l'attribution au mot).
                    if not spk_segs and (audio_duree or 0) > DIAR_INPROC_MAX_MIN * 60:
                        # v3.4.0 — GARDE ANTI-OOM : le repli in-process lit tout le WAV
                        # + ~1 inférence CAM++/s (fuite ONNX ~10 Go+ à 2h). Sur une
                        # réunion longue (voie sous-process KO), on DÉGRADE proprement
                        # en « sans diarisation » plutôt que crasher. La transcription
                        # reste complète, affichée en continu.
                        print(f"[reunion] repli diarisation in-process SAUTÉ "
                              f"(> {DIAR_INPROC_MAX_MIN} min, garde RAM) -> texte continu.")
                    elif not spk_segs:
                        try:
                            # relais RAM : recharger au finally SEULEMENT si un
                            # modèle CT2 était chargé (sur MLX, model est None).
                            if not _reloaded:
                                _reloaded = getattr(appstate.controller.engine, "model",
                                                    None) is not None
                            appstate.controller.engine.unload_model()
                        except Exception:
                            pass
                        try:
                            # v26 — réutilise l'instance de la voie guidée si elle
                            # existe (même extracteur CAM++ ; sinon fuite RAM).
                            if dia is None:
                                dia = diarizer.Diarizer()
                            spk_segs = dia.diarize(
                                wav_path, num_speakers=n_spk_set,
                                max_speakers=MAX_AUTO_SPEAKERS,
                                on_progress=lambda p: _ui("setMeetingProgress(%.2f)" % p))
                            voices_map = dia.last_voices()
                            diar_obj = dia
                        except Exception as e:
                            print(f"[reunion] diarisation repli KO ({e}).")
                            spk_segs = None
                    if spk_segs:
                        # Fusion au MOT (timestamps mot Whisper) -> les tours courts
                        # et le va-et-vient rapide sont capturés (au lieu d'attribuer
                        # un segment entier, qui noyait les interjections).
                        units = diarizer.words_from_segments(segments)
                        # Attribution au MOT (≥98 % mesuré sur réunions réelles),
                        # replis successifs : cf. _attribute_blocks.
                        blocks = _attribute_blocks(diar_obj, units, wav_path,
                                                   spk_segs)
                        # v1.0.12 — VISIO : réétiquetage par SOURCE (micro=Toi,
                        # système=interlocuteur) au lieu de la diarisation du mix
                        # (CHECK 3). Détecté par le sidecar dominance .dom ->
                        # visio UNIQUEMENT, présentiel strictement intact.
                        _is_visio = False
                        try:
                            _domp = (wav_path or "") + ".dom"
                            if blocks and os.path.exists(_domp):
                                blocks = _visio_relabel_blocks(blocks, _domp)
                                voices_map = {}     # centroïdes du mix non fiables -> pas d'apprentissage
                                _is_visio = True
                                print(f"[visio] réétiquetage par source -> {len(blocks)} blocs")
                        except Exception as _e:
                            print(f"[visio] réétiquetage dominance ignoré : {_e}")
                        if glossary:
                            for b in blocks:
                                b["text"] = processor.apply_glossary(b["text"], glossary)
                        n_spk = len({b["speaker"] for b in blocks})
                        # WhoTalks v1.2 — RECONNAISSANCE des voix connues (SAUTÉE en
                        # visio : centroïdes du mix non fiables ; cf. _recognize_voices,
                        # log=True : seul ce chemin imprime le détail).
                        auto_names = ({"SPEAKER_00": "Toi", "SPEAKER_01": "Interlocuteur"}
                                      if _is_visio
                                      else _recognize_voices(voices_map, log=True))
                        # v1.0.23 — FIABILITÉ D'ATTRIBUTION, distincte de la
                        # confiance de TRANSCRIPTION (`avg_confidence`, qui est
                        # la moyenne des probabilités des mots Whisper). Les deux
                        # étaient confondues : une réunion dont les locuteurs
                        # étaient largement mal attribués affichait quand même
                        # « 86 % », ce qui rassurait à tort. On stocke donc le
                        # score propre à l'attribution (part du vote gagnant,
                        # pondérée par la durée) pour pouvoir le dire à l'utilisateur.
                        _attr = getattr(diar_obj, "_last_attr_conf", None)
                        speaker_blocks_json = json.dumps(
                            {"blocks": blocks, "names": auto_names,
                             "voices": voices_map,
                             "attr_conf": _attr}, ensure_ascii=False)
                        print(f"[reunion] diarisation : {n_spk} locuteur(s), "
                              f"{len(blocks)} blocs en {_t.perf_counter()-td0:.1f}s"
                              + (f", fiabilité attribution {100*_attr:.0f} %."
                                 if _attr is not None else "."))
                    else:
                        print("[reunion] diarisation vide -> affichage continu.")
                finally:
                    # Libération GARANTIE de CAM++ (live OU repli) + rechargement
                    # de turbo, sur toutes les sorties du bloc diarisation
                    # (sinon fuite RAM cumulée sur réunions successives -> OOM
                    # sur 8 Go, ou Whisper resté déchargé).
                    # INCONDITIONNEL (revue v26) : un cas limite (guidé vide +
                    # live < 3 empreintes) laissait l'extracteur du live chargé
                    # jusqu'au GC. unload() est idempotent -> toujours libérer.
                    if wt_diar is not None:
                        try:
                            wt_diar.unload()
                        except Exception:
                            pass
                    if dia is not None:
                        try:
                            dia.unload()
                        except Exception:
                            pass
                    if _reloaded:
                        try:
                            appstate.controller.engine.load_model()
                        except Exception:
                            pass
            else:
                print("[reunion] diarisation indisponible (sherpa/modèles absents)"
                      " -> affichage continu.")
        except Exception as e:
            print(f"[reunion] diarisation ignorée ({e}) -> affichage continu.")
            speaker_blocks_json = None

    if appstate.store and meeting_id:
        fields = {"transcription_structuree": structured, "status": "ready"}
        if conf_json is not None:
            fields["confidence_json"] = conf_json
            fields["avg_confidence"] = avg_conf
        if speaker_blocks_json is not None:
            fields["speaker_blocks_json"] = speaker_blocks_json
        appstate.store.update_meeting(meeting_id, **fields)
        try:    # v1.2.0 — compteur d'usage : mots transcrits et durée d'audio
            appstate.store.record_meeting(appstate.store.word_count(structured or ""), audio_duree)
            _save_settings({"last_used_at": time.time()})
            if appstate.telemetry is not None:
                appstate.telemetry.notify_usage()
        except Exception:
            pass
        _obsidian_meeting_sync(meeting_id)   # v1.1.0 — coffre « Vlocal, ma voix »

    # Notif + refresh UI.
    mins = int(audio_duree // 60)
    secs = int(audio_duree % 60)
    duree_txt = (f"{mins} min" if mins else f"{secs} s")
    notifier.show("Réunion prête",
                  f"Transcription terminée ({duree_txt}).",
                  subtitle=None)
    _toast("Réunion prête. Cliquez dessus dans la liste pour la lire ou la "
           "copier.", kind="success")
    _ui("setMeetingStage(%s)" % json.dumps("done"))
    _refresh_lists_ui()


def _total_ram_gb():
    """v22.3 — RAM PHYSIQUE totale (Go). On NE se fie PAS à la mémoire « dispo »
    de psutil : macOS la sur-estime (compression mémoire) -> on choisissait
    batch 4 sur une machine pourtant chargée, d'où swap/ventilateurs. La RAM
    physique est la borne dure de la cible « 8 Go avec d'autres apps »."""
    try:
        import subprocess as _sp
        out = _sp.run(["sysctl", "-n", "hw.memsize"], capture_output=True,
                      text=True, timeout=2).stdout.strip()
        return int(out) / 2 ** 30
    except Exception:
        try:
            import psutil as _ps
            return _ps.virtual_memory().total / 2 ** 30
        except Exception:
            return 8.0   # hypothèse PRUDENTE (cible basse) si indéterminé
