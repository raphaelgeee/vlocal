"""Moteur de transcription vu de l'app : modèles, chargement, mémoire.

Résout les dossiers des modèles (cache téléchargé ou bundle), charge le moteur
Whisper en tâche de fond après l'ouverture de la fenêtre, crée le contrôleur de
dictée, applique la politique mémoire (memory_policy.py : déchargement sous
pression) et relance le chargement après un téléchargement des modèles.
Contient aussi l'auto-test sans fenêtre (--selftest).
"""
import os
import subprocess
import threading
import time

import appstate
import errors as _EV
import model_store
import telemetry
from dictation import DictationController, _start_dictation_supervisor
from engine import (
    SAMPLE_RATE,
    SMALL_DIR_NAME,
    TURBO_DIR_NAME,
    VlocalEngine,
    assemble_segments,
)
from lifecycle import _watch_system_wake
from meetings import _meeting_state
from settings import _load_settings, _onboarding_done, _save_settings
from ui_bridge import _toast


def _resolve_model_dirs():
    """(Re)calcule MODEL_DIR/SMALL_MODEL_DIR depuis model_store. Appelé à l'import,
    en main(), et après un téléchargement (les modèles passent du néant au cache)."""
    _root = model_store.models_dir()
    appstate.model_dir = os.path.join(_root, TURBO_DIR_NAME)
    appstate.small_model_dir = os.path.join(_root, SMALL_DIR_NAME)


def _point_mlx_to_cache():
    """Pointe le moteur GPU (mlx) sur le modèle q8 du dossier résolu (cache ou
    bundle) au lieu de sa résolution bundle interne. Sans effet si le q8 est absent
    (mlx gardera son repli). N'importe mlx_engine que si le modèle existe."""
    try:
        _q8 = os.path.join(model_store.models_dir(), "whisper-large-v3-turbo-mlx-q8")
        if os.path.exists(os.path.join(_q8, "config.json")):
            import mlx_engine as _mlx_set
            _mlx_set._model_ref = _q8
    except Exception:
        pass


def _refresh_whisper_bias():
    """v15 — Réinjecte les formes correctes du glossaire dans le biais
    hotwords de Whisper (passe 1, v20). Appelé après toute modif du glossaire."""
    if appstate.store is None:
        return
    try:
        from engine import set_correction_terms
        set_correction_terms([c for _, c in appstate.store.glossary_pairs()])
    except Exception:
        pass


def _telemetry_profile() -> dict:
    """Profil technique transmis par la télémétrie (telemetry.machine_profile).
    Appelé par telemetry.sync_once APRÈS la vérification du consentement.
    Moteur : celui de la dernière dictée ("" si le moteur n'est pas encore chargé)."""
    ctrl = appstate.controller
    return telemetry.machine_profile(appstate.ui_lang, _load_settings().get("hotkey"),
                                     ctrl.engine.last_backend() if ctrl is not None else "")


# --------------------------------------------------------------------------- #
def selftest():
    """Validation sans fenêtre ni micro :
      1) preuve du fix des espaces (segments synthétiques SANS espace de tête)
      2) transcription FINALE multi-phrases (espaces/ponctuation corrects)
      3) mesure latence aperçu (slice) vs final, sur l'audio `say`.
    """
    import wave

    import numpy as np

    print("Vlocal — auto-test v2 (sans fenêtre, sans micro)")
    print()

    # 1) Preuve déterministe du fix des espaces (cas qui causait "motscollés")
    glued = ["bonjour", "salut", "comment ça va", ", c'est un test."]
    fixed = assemble_segments(glued)
    print("1) assemble_segments(sans espaces) ->", repr(fixed))
    assert fixed == "bonjour salut comment ça va, c'est un test.", fixed
    print("   [ok] espaces ajoutés + ponctuation collée correctement")
    print()

    # Audio multi-phrases. v16 — écrit dans un dossier TEMP (jamais dans
    # BASE_DIR : en .app empaqueté ce serait dans le bundle, ce qui casse la
    # signature de code).
    import tempfile as _tf
    audio_file = os.path.join(_tf.gettempdir(), "vlocal_test_multi.wav")
    if not os.path.exists(audio_file):
        subprocess.run(
            ["say", "-o", audio_file, "--data-format=LEI16@16000",
             "Bonjour. Comment allez-vous aujourd'hui ? Ceci est un test de dictée vocale, cent pour cent locale. Merci beaucoup."],
            check=True,
        )

    eng = VlocalEngine(model_dir=appstate.model_dir)
    with wave.open(audio_file, "rb") as w:
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    audio = pcm.astype(np.float32) / 32768.0
    dur = len(audio) / SAMPLE_RATE

    # 2) FINAL
    t0 = time.perf_counter()
    final = eng.transcribe(audio, vad=True)
    dt_final = time.perf_counter() - t0
    print(f"2) FINAL ({dur:.1f}s audio, vad=on) en {dt_final:.2f}s :")
    print("   ", repr(final))
    ok = "dictée" in final.lower() and "locale" in final.lower() and "  " not in final
    print("   [ok] texte propre" if ok else "   [ko] problème d'espaces/contenu")
    print(f"   Mesure (cette machine) : transcription finale ≈ {dt_final:.2f}s")
    return 0 if ok else 1


def _post_startup_model_checks():
    """v15 — Tourne ~1,2 s après le démarrage : message de bienvenue au tout
    premier lancement. (Aucun appel réseau : Vlocal ne contacte rien, même pas
    en localhost — l'ancien sondage Ollama a été retiré, le SLM n'existe plus.)
    v1.0.3 — repoussé tant que l'app n'est pas activée : sinon le toast s'affiche
    DERRIÈRE l'écran d'activation (invisible) et welcome_shown passerait quand
    même à True, donc le message ne serait jamais revu après activation."""
    try:
        if not _onboarding_done() or model_store.needs_download():
            threading.Timer(2.0, _post_startup_model_checks).start()
            return
        s = _load_settings()
        if not s.get("welcome_shown"):
            # v1.0 — utilise le libellé RÉEL du raccourci (défaut Ctrl + Cmd),
            # plus le « Ctrl + Espace » codé en dur qui était faux.
            _hk = (appstate.hotkey_label or "").strip()
            if appstate.ui_lang == "en":
                _how = ('click "Dictate" (or hold %s)' % _hk) if _hk else 'click "Dictate"'
                _toast('Welcome to Vlocal. Choose a mode above, then %s and speak. '
                       'Everything stays on your Mac, nothing is sent online.'
                       % _how, kind="info")
            else:
                _how = ("cliquez sur « Dicter » (ou maintenez %s)" % _hk) if _hk \
                    else "cliquez sur « Dicter »"
                _toast("Bienvenue dans Vlocal. Choisissez un mode ci-dessus, puis "
                       "%s et parlez. Tout reste sur votre Mac, rien n'est "
                       "envoyé en ligne." % _how, kind="info")
            _save_settings({"welcome_shown": True})
    except Exception:
        pass


_engine_ready = threading.Event()


# Phase 2 (RAM), v1.6 — DÉCHARGEMENT SOUS PRESSION MÉMOIRE : les modèles
# restent résidents tant que macOS n'est pas sous pression (plus de rechargement
# ni de transition Metal à chaque pause) ; sous pression, le modèle CPU part
# d'abord, puis le modèle GPU. Politique et seuils : memory_policy.py.
_MEMORY_POLL_S = 5.0       # sysctl + psutil : ~0,2 ms par passage


def _apply_memory_plan(eng, actions) -> None:
    """Exécute les déchargements décidés par memory_policy.plan, dans l'ordre."""
    import mlx_engine as _mlx
    for action in actions:
        try:
            if action == "cpu":
                eng.unload_model()
                print("[mémoire] modèle CPU déchargé (rechargé au prochain usage).")
            elif action == "gpu":
                _mlx.unload()
                print("[mémoire] modèle GPU déchargé (rechargé dès le début de "
                      "la prochaine dictée).")
        except Exception as e:
            print(f"[mémoire] déchargement « {action} » KO : {type(e).__name__}: {e}")


def _memory_tick() -> None:
    """Un passage de la politique mémoire : lit la pression, décide, exécute."""
    import time as _t
    import memory_policy as _mp
    import mlx_engine as _mlx
    eng = appstate.controller.engine if appstate.controller is not None else None
    if eng is None:
        return
    busy = bool(_meeting_state.get("recording")
                or _meeting_state.get("pipeline_busy")
                or _meeting_state.get("file_busy")
                or appstate.controller.recording_active())
    now = _t.time()
    cpu_last = max(getattr(eng, "last_use", 0.0), getattr(eng, "_fast_last_use", 0.0))
    level = _mp.pressure_level()
    actions = _mp.plan(
        level, busy=busy,
        gpu_loaded=_mlx.loaded(), gpu_idle_s=_mlx.idle_seconds(),
        cpu_loaded=(eng.model is not None or eng._fast_model is not None),
        cpu_idle_s=now - cpu_last,
        cpu_is_fallback=(_mlx.status()[0] == "ok"))
    if actions:
        print(f"[mémoire] pression {_mp.LEVEL_NAMES[level]} -> "
              f"déchargement : {', '.join(actions)}.")
        _apply_memory_plan(eng, actions)


def _start_idle_unloader():
    def _check():
        try:
            _memory_tick()
        except Exception as e:
            print(f"[mémoire] passage KO : {type(e).__name__}: {e}")
        finally:
            t = threading.Timer(_MEMORY_POLL_S, _check)
            t.daemon = True
            t.start()
    t = threading.Timer(60.0, _check)
    t.daemon = True
    t.start()


def _finish_model_download():
    """v3.3 — après le téléchargement R2 des modèles : recalcule les chemins puis
    lance le moteur Whisper en tâche de fond (comme au démarrage normal)."""
    _resolve_model_dirs()
    _point_mlx_to_cache()
    # v1.4.4 — un verdict GPU rendu AVANT le téléchargement (modèle absent) est
    # caduc : on l'oublie, la prochaine dictée re-sonde. Après le repointage :
    # reset_availability garde une référence de modèle qui existe (le cache).
    import mlx_engine as _mlx_reset
    _mlx_reset.reset_availability("modèle téléchargé")
    _wd = None
    if os.path.exists(os.path.join(appstate.model_dir, "model.bin")):
        _wd = appstate.model_dir
    elif os.path.exists(os.path.join(appstate.small_model_dir, "model.bin")):
        _wd = appstate.small_model_dir
    if _wd is None:
        print("[models] post-download : modèle introuvable malgré le téléchargement.")
        return False
    appstate.engine_load_params = (_wd, appstate.cpu_threads)
    threading.Thread(target=_load_engine_async, daemon=True).start()
    return True


def _load_engine_async():
    """Charge le moteur Whisper en tâche de fond (la fenêtre est déjà affichée).
    Met à jour _controller + injecte le glossaire + signale l'UI quand prêt."""
    if not appstate.engine_load_params:
        return
    whisper_dir, cpu_threads = appstate.engine_load_params
    print(f"[whisper] chargement en arrière-plan "
          f"({os.path.basename(whisper_dir)}, int8, cpu_threads={cpu_threads})…")
    try:
        engine = VlocalEngine(model_dir=whisper_dir, cpu_threads=cpu_threads)
    except Exception as e:
        print(f"[whisper] échec chargement moteur : {e}")
        if _EV: _EV.log(_EV.E.ENGINE_LOAD_FAIL, err=type(e).__name__)   # S2 — capté par la tour de contrôle
        _toast("Le moteur de transcription n'a pas pu démarrer. Réessayez de "
               "lancer Vlocal.", kind="error")
        return
    appstate.controller = DictationController(engine)
    # Glossaire -> biais lexical (hotwords)
    try:
        if appstate.store is not None:
            from engine import set_correction_terms
            terms = [c for _, c in appstate.store.glossary_pairs()]
            set_correction_terms(terms)
            print(f"[glossaire] {len(terms)} terme(s) injecté(s).")
    except Exception as e:
        print(f"[glossaire] injection KO : {e}")
    try:
        import psutil as _ps
        rss = _ps.Process().memory_info().rss / 1e9
        print(f"[whisper] prêt — turbo en mémoire, RAM {rss:.2f} Go.")
    except Exception:
        print("[whisper] prêt.")
    _engine_ready.set()
    _start_dictation_supervisor()   # v1.0.9 — auto-heal : la dictée ne reste jamais bloquée
    # v1.0.22 — MICRO BÉTON : préchauffe le worker micro (process jetable) pour
    # que la 1re dictée n'attende pas son spawn. Best-effort, en thread.
    try:
        threading.Thread(target=engine.prewarm_mic_worker,
                         name="micworker-prewarm", daemon=True).start()
    except Exception:
        pass
    _watch_system_wake(engine)
    _start_idle_unloader()   # libère Whisper de la RAM après inactivité prolongée
    # v2 — Préchargement de small EN FOND si la dictée l'utilisera (modes
    # « auto » et « rapide »), pour que la PREMIÈRE dictée soit déjà rapide
    # (sinon elle paie ~1 s de chargement small et ça ressemble à une latence).
    # Mode « fidele » (turbo seul) -> on ne charge PAS small (zéro RAM gaspillée).
    try:
        speed = _load_settings().get("dictation_speed", "auto")
    except Exception:
        speed = "auto"
    def _preload_and_warm():
        try:
            # v23.1 RAM — quand le GPU MLX est actif, la dictée passe EXCLUSIVEMENT
            # par lui (transcribe_adaptive court-circuite la cascade CPU). Le
            # modèle small CPU (~480 Mo) ne servirait JAMAIS : on évite de le
            # précharger. Économie nette ~480 Mo, zéro impact dictée. (En repli
            # CPU pur — Intel/Windows — on garde le préchargement small.)
            try:
                import mlx_engine as _mlx
                _gpu = _mlx.available()
            except Exception:
                _gpu = False
            if speed in ("auto", "rapide") and not _gpu:
                if engine._ensure_fast_model():
                    print("[whisper] small préchargé en fond (dictée « auto »/« rapide »).")
            # PRÉCHAUFFE : amortit la 1re inférence (cold-start) -> la PREMIÈRE
            # dictée réelle est aussi rapide que les suivantes.
            engine.warmup()
        except Exception:
            pass
    threading.Thread(target=_preload_and_warm, daemon=True).start()
