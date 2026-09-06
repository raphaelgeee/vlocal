#!/usr/bin/env python3
"""
Vlocal — Backend de transcription GPU (MLX / Metal), OPTIONNEL, repli CPU auto.

Le MÊME modèle Whisper large-v3-turbo, exécuté sur le GPU Apple via MLX, mesuré
7 à 26x plus rapide que le CPU int8 (faster-whisper) à QUALITÉ IDENTIQUE (0,0 %
d'écart de texte sur corpus apparié ; 0-4 % sur réunions réelles, texte au
caractère près). RAM ~équivalente (2,2-2,6 Go), word-timestamps disponibles.

ROBUSTESSE : tout est à repli silencieux. Si mlx/mlx_whisper sont absents (build
sans MLX), si le GPU est indisponible, ou si une inférence lève -> available()
renvoie False et l'appelant retombe sur le moteur CPU (faster-whisper) inchangé.
Désactivable de force par la variable d'environnement VLOCAL_NO_MLX=1.

macOS Apple Silicon uniquement (MLX = Metal). Aucun appel réseau au runtime
(le modèle est soit bundlé dans l'app, soit dans le cache local).
"""
import os
import sys

# Dépôt MLX officiel du turbo (poids fp16, ~1,5 Go) — utilisé en DEV (cache HF).
# En production, on préfère le dossier bundlé dans l'app (cf. _model_ref).
_HF_REPO = "mlx-community/whisper-large-v3-turbo"

_available = None     # None = pas encore testé ; True/False ensuite (cache)
_model_ref = None     # chemin du modèle (dossier bundlé) ou id du dépôt HF
_last_use = 0.0       # horodatage de la dernière inférence (idle-unload)
_kernels_warm = False  # True dès que les kernels Metal ont été compilés une fois

# v1.1.0 : SUSPENSION TEMPORAIRE du GPU après un timeout d'inférence, au lieu
# d'une bascule CPU définitive. Une session Vlocal dure des semaines (l'app ne
# redémarre jamais) : condamner le GPU au premier dépassement de délai coûtait
# x8 sur toutes les dictées suivantes (mesuré : 1,0 s GPU contre 8,0 s CPU) et
# 1,5 Go de RAM. On suspend le GPU _GPU_COOLDOWN_S, puis on le retente ; il n'est
# abandonné pour la session qu'après _GPU_MAX_STRIKES gels en _GPU_STRIKE_WINDOW_S.
_GPU_COOLDOWN_S = 600.0
_GPU_STRIKE_WINDOW_S = 3600.0
_GPU_MAX_STRIKES = 3
_gpu_suspended_until = 0.0
_gpu_strikes = []

# v23.1.2 — EXÉCUTEUR MONO-THREAD pour TOUTES les opérations GPU MLX.
#
# POURQUOI : MLX n'est PAS thread-safe — ses streams Metal sont PAR THREAD. L'app
# appelle le préchauffe (thread du prewarm, au début de la parole) et la
# transcription (thread _finalize, à la relâche) depuis des threads DIFFÉRENTS.
# Si le modèle est chargé dans un thread puis utilisé dans un autre :
#   RuntimeError: There is no Stream(gpu, N) in current thread
#   [METAL] Command buffer execution failed: Invalid Resource
# -> la transcription MLX lève -> l'app retombe en repli CPU (lent, +1,5 Go).
#
# SOLUTION : un unique thread « worker » exécute warmup/transcribe/unload. Le
# modèle est donc TOUJOURS créé ET utilisé sur le MÊME thread -> streams cohérents.
# Cela sérialise aussi naturellement les opérations (plus besoin de verrou).
import threading as _threading
import queue as _queue

_mlx_jobs = _queue.PriorityQueue()
_mlx_worker = None
_mlx_worker_lock = _threading.Lock()
# v1.0.22 — FILE PRIORITAIRE : la DICTÉE (l'utilisateur attend, curseur clignotant)
# ne doit jamais patienter derrière un préchauffe, un bloc d'import de 300 s
# d'audio ou une fenêtre de réunion. PriorityQueue ordonnée sur (prio, seq) :
# prio 0 = dictée, prio 1 = tout le reste ; seq préserve le FIFO à prio égale.
# Aucun impact qualité : c'est l'ORDRE de passage sur le GPU qui change.
PRIO_DICTATION = 0
PRIO_BACKGROUND = 1
_mlx_seq = 0
_mlx_seq_lock = _threading.Lock()


def _next_seq() -> int:
    global _mlx_seq
    with _mlx_seq_lock:
        _mlx_seq += 1
        return _mlx_seq


# v29.1 — TIMEOUT DUR du worker MLX. Une opération Metal normale (préchauffe
# ~3-5 s, dictée <1 s, bloc réunion 5 min ~5-15 s) est très en deçà. Au-delà =
# inférence Metal FIGÉE (vu sous dictée intensive : le churn décharge/recharge
# finit par bloquer un command buffer). Sans timeout, done.wait() bloquait à
# l'infini -> _do_finalize ne rendait jamais la main -> verrou _busy coincé 180 s
# (« dictée infinie »). On borne à 60 s et on RÉINITIALISE le worker.
_MLX_JOB_TIMEOUT = 60.0


def _worker_loop(jobs):
    while True:
        item = jobs.get()
        if item is None:                  # sentinelle (jamais utilisée, garde)
            return
        _prio, _seq, fn, args, kwargs, box, done, started = item
        try:
            started.set()      # v1.0.22 : le job a QUITTÉ la file (voir _on_worker)
            box["value"] = fn(*args, **kwargs)
        except BaseException as e:        # y compris les erreurs Metal
            box["error"] = e
        finally:
            done.set()


def _on_worker(fn, *args, _timeout=_MLX_JOB_TIMEOUT, _prio=PRIO_BACKGROUND, **kwargs):
    """Exécute fn sur l'UNIQUE thread MLX et renvoie son résultat (ou relève).
    Si fn ne rend pas la main sous _timeout (Metal figé), on ABANDONNE le thread
    (daemon : il restera coincé sur l'op bloquée puis dormira sur son ancienne
    file) et on force un worker NEUF au prochain appel -> la dictée suivante
    repart, au lieu d'un blocage 180 s. Lève TimeoutError pour que l'appelant
    libère son verrou _busy dans son finally."""
    global _mlx_worker, _mlx_jobs, _kernels_warm
    with _mlx_worker_lock:
        if _mlx_worker is None or not _mlx_worker.is_alive():
            _mlx_jobs = _queue.PriorityQueue()
            _mlx_worker = _threading.Thread(
                target=_worker_loop, args=(_mlx_jobs,), name="mlx-gpu",
                daemon=True)
            _mlx_worker.start()
        jobs = _mlx_worker, _mlx_jobs
    box, done, started = {}, _threading.Event(), _threading.Event()
    jobs[1].put((_prio, _next_seq(), fn, args, kwargs, box, done, started))
    if not done.wait(_timeout):
        # v1.0.22 — DISTINGUER L'ATTENTE EN FILE DU VRAI GEL METAL. Le timeout
        # englobait aussi le temps passé À ATTENDRE SON TOUR derrière un autre
        # job (bloc d'import de 300 s d'audio, fenêtre de réunion, préchauffe).
        # Une dictée mise en file derrière eux déclenchait donc `_force_cpu`,
        # qui désactive le GPU pour TOUTE la session : +1,7 Go de RAM et x7 sur
        # la vitesse de TOUTES les dictées suivantes, à cause d'un simple
        # embouteillage. Désormais, on ne condamne le GPU que si le job avait
        # RÉELLEMENT commencé (started) ; sinon on rend la main sans rien casser.
        _really_stuck = started.is_set()
        if _really_stuck:
            # FIGEAGE : on jette ce worker. Le prochain _on_worker en recrée un
            # neuf (file neuve) ; le modèle sera rechargé proprement.
            with _mlx_worker_lock:
                if _mlx_worker is jobs[0]:   # pas déjà remplacé par un autre appel
                    _mlx_worker = None
                    _mlx_jobs = _queue.PriorityQueue()   # v1.0.22 : même type
            _kernels_warm = False            # re-préchauffe propre au prochain usage
            # v1.1.0 : suspension temporaire (voir _gpu_strike), plus de bascule
            # définitive au premier dépassement. L'appelant a son repli CPU.
            _gpu_strike(f"inférence MLX figée (>{_timeout:.0f}s)")
            raise TimeoutError(
                "transcription MLX figée (>%.0fs) — worker réinitialisé" % _timeout)
        raise TimeoutError(
            "GPU occupé (>%.0fs) — travail en attente, GPU conservé" % _timeout)
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _base_dir():
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.dirname(os.path.abspath(__file__))


def _diag(msg: str) -> None:
    """Trace persistante du verdict GPU. L'app gelée (GUI) perd son stdout :
    sans ça, impossible de savoir si le GPU s'active vraiment. Best-effort,
    ne lève jamais. Fichier : ~/Library/Logs/Vlocal/mlx.log."""
    try:
        import time
        d = os.path.expanduser("~/Library/Logs/Vlocal")
        os.makedirs(d, exist_ok=True)
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(os.path.join(d, "mlx.log"), "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass


def _force_cpu(reason: str) -> None:
    """v3.2.8 — Bascule DÉFINITIVE sur CPU pour la session. Un vrai échec Metal
    (préchauffe KO, inférence figée) = GPU/metallib inutilisable : on ARRÊTE de
    retenter le GPU à chaque dictée (coût + latence + risque de re-figeage) et on
    rend la main au moteur CPU. Plug-and-play : le client garde une app qui marche."""
    global _available
    _available = False
    print(f"[mlx] bascule CPU définitive (session) : {reason}")
    _diag(f"MLX -> bascule CPU définitive (session) : {reason}")
    # v1.0.22 — TÉLÉMÉTRIE DU TROU NOIR. 7 gels GPU (>= 60 s d'attente chacun,
    # puis toute la session en CPU) sont survenus entre le 16/06 et le 10/08 :
    # AUCUN n'apparaît dans le journal d'événements, parce que le seul site
    # d'émission dépendait d'une exception que ce chemin n'a jamais levée. La
    # panne la plus punitive du produit était donc invisible au diagnostic.
    try:
        import errors as _EV
        _EV.log(_EV.E.GPU_FALLBACK_CPU, reason=str(reason)[:120])
    except Exception:
        pass


def _gpu_strike(reason: str) -> None:
    """v1.1.0 : enregistre un gel GPU. Suspend le GPU _GPU_COOLDOWN_S (le moteur
    CPU prend le relais, l'utilisateur n'attend jamais) ; au-delà de
    _GPU_MAX_STRIKES gels dans la fenêtre glissante, bascule définitive."""
    global _gpu_suspended_until, _gpu_strikes
    import time
    now = time.time()
    _gpu_strikes = [t for t in _gpu_strikes if now - t < _GPU_STRIKE_WINDOW_S] + [now]
    if len(_gpu_strikes) >= _GPU_MAX_STRIKES:
        _force_cpu(f"{reason}, {len(_gpu_strikes)} gels en moins d'une heure")
        return
    _gpu_suspended_until = now + _GPU_COOLDOWN_S
    msg = (f"GPU suspendu {_GPU_COOLDOWN_S / 60:.0f} min ({reason}), "
           f"gel {len(_gpu_strikes)}/{_GPU_MAX_STRIKES}")
    print(f"[mlx] {msg}")
    _diag(msg)
    try:
        import errors as _EV
        _EV.log(_EV.E.GPU_FALLBACK_CPU, reason=str(msg)[:120])
    except Exception:
        pass


def model_ref():
    """Modèle MLX à charger : le dossier BUNDLÉ s'il existe (app empaquetée,
    100 % local), sinon l'id du dépôt HF (DEV : téléchargé une fois dans le
    cache local ~/.cache/huggingface)."""
    global _model_ref
    if _model_ref is not None:
        return _model_ref
    base = _base_dir()
    # v23.1 — préférer le modèle QUANTIFIÉ q8 : sortie texte STRICTEMENT identique
    # au fp16 (0,00 % WER mesuré) mais RAM /2 (1543 -> 833 Mo) -> dictée sous 2 Go.
    # Repli fp16 si q8 absent (dev avant quantize_model.py), puis dépôt HF (dev).
    for _name in ("whisper-large-v3-turbo-mlx-q8", "whisper-large-v3-turbo-mlx"):
        _d = os.path.join(base, "models", _name)
        if os.path.exists(os.path.join(_d, "config.json")):
            _model_ref = _d
            return _model_ref
    _model_ref = _HF_REPO
    return _model_ref


def available() -> bool:
    """True si le backend GPU MLX est utilisable. Cache le résultat. Repli
    silencieux total : toute exception -> False -> moteur CPU."""
    global _available, _gpu_suspended_until
    if _available is not None:
        if _available and _gpu_suspended_until:
            import time
            if time.time() < _gpu_suspended_until:
                return False
            _gpu_suspended_until = 0.0
            print("[mlx] fin de suspension : GPU réactivé.")
            _diag("fin de suspension : GPU réactivé.")
        return _available
    if os.environ.get("VLOCAL_NO_MLX"):
        _available = False
        _diag("MLX désactivé par VLOCAL_NO_MLX -> moteur CPU.")
        return False
    if sys.platform != "darwin":
        _available = False
        _diag(f"MLX indisponible (plateforme {sys.platform}) -> moteur CPU.")
        return False
    try:
        import mlx.core as mx           # noqa: F401
        import mlx_whisper              # noqa: F401
        # En frozen, on exige le modèle bundlé (pas de téléchargement runtime,
        # promesse 100 % local). En dev, le cache HF est toléré.
        if getattr(sys, "frozen", False):
            ref = model_ref()
            if not os.path.isdir(ref):
                _available = False
                _diag(f"MLX KO : modèle bundlé absent ({ref}) -> moteur CPU.")
                return False
        # v3.2.8 — SMOKE-TEST GPU RÉEL (pas seulement l'import). Une metallib
        # incompatible (cible Metal trop récente pour ce macOS) charge à l'import
        # mais ÉCHOUE au 1er kernel : sans ce test, available() renvoyait True et la
        # bascule CPU n'arrivait qu'à la 1re dictée du client (mauvaise surprise).
        # Ici un kernel minuscule force le chargement de la metallib + une exécution
        # Metal -> si KO, on bascule proprement sur CPU DÈS LE DÉMARRAGE.
        import mlx.core as _mxp
        _probe = float(_mxp.sum(_mxp.array([1.0, 2.0, 3.0])))
        if _probe != 6.0:
            raise RuntimeError(f"probe GPU incohérent ({_probe})")
        _available = True
        _diag(f"MLX DISPONIBLE (GPU Metal vérifié — probe OK). modèle={model_ref()}")
    except Exception as e:
        print(f"[mlx] indisponible ({e}) -> moteur CPU.")
        _diag(f"MLX KO ({type(e).__name__}: {e}) -> moteur CPU.")
        _available = False
    return _available


def _warmup_impl() -> None:
    global _last_use, _kernels_warm
    try:
        if loaded():
            return                      # déjà chaud : rien à faire
        if not _kernels_warm:
            # 1er appel : inférence à blanc -> compile les kernels Metal, puis
            # vidage du scratch. (Tout est sur le worker, jamais concurrent.)
            import numpy as np
            import mlx_whisper
            buf = (np.random.randn(8000).astype(np.float32) * 0.01)
            mlx_whisper.transcribe(buf, path_or_hf_repo=model_ref(),
                                   language="fr", fp16=True)
            _kernels_warm = True
            try:
                import mlx.core as mx
                mx.clear_cache()
            except Exception:
                pass
            print("[mlx] GPU préchauffé (kernels Metal compilés).")
        else:
            # Rechargements suivants : charger les poids seuls (kernels déjà
            # compilés) -> dictée suivante instantanée, RAM rendue entre-temps.
            from mlx_whisper.transcribe import ModelHolder
            import mlx.core as mx
            ModelHolder.get_model(model_ref(), mx.float16)
            print("[mlx] modèle GPU rechargé (poids seuls).")
    except Exception as e:
        print(f"[mlx] préchauffe KO (sans gravité) : {e}")
        # v3.2.8 — si l'échec est une VRAIE erreur Metal (GPU/metallib cassé), on ne
        # retente pas le GPU à chaque dictée : bascule CPU définitive pour la session.
        _es = str(e).lower()
        if any(k in _es for k in ("metal", "command buffer", "metallib",
                                  "library", "no stream", "device", "gpu")):
            _force_cpu(f"préchauffe Metal KO ({type(e).__name__})")
    finally:
        import time as _t
        _last_use = _t.time()


def warmup() -> None:
    """Amorce le GPU pour que la dictée suivante soit instantanée. Appelé au
    démarrage ET au DÉBUT de la parole (prewarm) pour recharger le modèle si
    l'éco-RAM l'avait déchargé — masqué par la parole. Exécuté sur le worker MLX
    (cf. _on_worker) : le modèle est créé sur le MÊME thread que la transcription."""
    if not available():
        return
    _on_worker(_warmup_impl)


def _transcribe_impl(a, language, initial_prompt, word_timestamps,
                     hallucination_silence_threshold=None, temperature=None) -> dict:
    import time
    import mlx_whisper
    global _last_use
    try:
        # NB : les seuils qualité de mlx_whisper (température-fallback 0->1,
        # compression_ratio 2.4, logprob -1.0, no_speech 0.6) sont ACTIFS par
        # défaut. hallucination_silence_threshold (anti-hallucination sur
        # silences, ex. « Sous-titrage Société Radio-Canada ») n'est passé que
        # par la RÉUNION (word_timestamps requis) — None = dictée inchangée.
        kw = {}
        if hallucination_silence_threshold is not None and word_timestamps:
            kw["hallucination_silence_threshold"] = hallucination_silence_threshold
        # v3.3.1 — DICTÉE : ladder de température borné 6->4 niveaux (passé par
        # _mlx_text). Plafonne les pics de re-décodage (2-4 s) en gardant 2 chances
        # de récupération qualité. None (réunion) = défaut 6 niveaux inchangé.
        if temperature is not None:
            kw["temperature"] = temperature
        return mlx_whisper.transcribe(
            a, path_or_hf_repo=model_ref(), language=language,
            condition_on_previous_text=False,
            initial_prompt=(initial_prompt or None),
            word_timestamps=word_timestamps, fp16=True, **kw)
    finally:
        _last_use = time.time()
        # v23.1 RAM — libère le POOL de buffers Metal de l'inférence (~590 Mo
        # mesurés) après CHAQUE transcription. Modèle gardé chaud (« active »
        # inchangée), coût mesuré +8 ms. Sûr ici : on est sur le worker, donc
        # aucune autre inférence MLX ne tourne en parallèle.
        try:
            import mlx.core as mx
            mx.clear_cache()
        except Exception:
            pass


def transcribe(audio_np, language: str = "fr", initial_prompt=None,
               word_timestamps: bool = False,
               hallucination_silence_threshold=None, temperature=None,
               _timeout=_MLX_JOB_TIMEOUT, _prio=PRIO_BACKGROUND) -> dict:
    """Transcrit un buffer float32 mono 16 kHz sur le GPU. Renvoie le dict
    mlx_whisper brut {text, segments[...]}. Lève si MLX indisponible (l'appelant
    doit avoir vérifié available() et gérer le repli). Exécuté sur l'UNIQUE thread
    worker MLX -> pas de partage de modèle entre threads (sinon crash Metal).
    _timeout : plafond du worker pour CET appel. La DICTÉE en passe un plus court
    (< watchdog de finalisation) pour que le repli CPU parte AVANT que le watchdog
    ne libère le verrou -> jamais de chevauchement. La réunion garde le défaut 60 s."""
    import numpy as np
    a = np.asarray(audio_np, dtype=np.float32)
    return _on_worker(_transcribe_impl, a, language, initial_prompt,
                      word_timestamps, hallucination_silence_threshold, temperature,
                      _timeout=_timeout, _prio=_prio)


def idle_seconds() -> float:
    """Secondes écoulées depuis la dernière inférence MLX (0 si rien chargé)."""
    import time
    return (time.time() - _last_use) if (loaded() and _last_use) else 0.0


def loaded() -> bool:
    """True si le modèle MLX est actuellement RÉSIDENT en mémoire GPU."""
    try:
        from mlx_whisper.transcribe import ModelHolder
        return ModelHolder.model is not None
    except Exception:
        return False


def _unload_impl() -> None:
    try:
        from mlx_whisper.transcribe import ModelHolder
        ModelHolder.model = None
        ModelHolder.model_path = None
    except Exception:
        pass
    try:
        import gc
        gc.collect()
        import mlx.core as mx
        mx.clear_cache()      # vide le pool de buffers GPU MLX
    except Exception:
        pass


def unload() -> None:
    """v23 — LIBÈRE le modèle MLX de la mémoire GPU/unifiée.

    mlx_whisper cache le modèle dans ModelHolder.model et MLX retient les buffers
    GPU dans son pool ; sans déchargement, ~830 Mo restaient résidents à vie ->
    la « redescente de RAM » à l'inactivité était cassée. Best-effort.

    v23.1.2 — exécuté sur le worker MLX : le clear_cache se fait sur le MÊME
    thread que l'inférence, et la file sérialise -> jamais de déchargement en
    plein milieu d'une transcription (sinon crash Metal / repli CPU)."""
    _on_worker(_unload_impl)
