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
import time

# Dépôt MLX officiel du turbo (poids fp16, ~1,5 Go) — utilisé en DEV (cache HF).
# En production, on préfère le dossier bundlé dans l'app (cf. _model_ref).
_HF_REPO = "mlx-community/whisper-large-v3-turbo"

_available = None     # None = pas encore testé ; True/False ensuite (cache)
_model_ref = None     # chemin du modèle trouvé (dossier bundlé ou cache pointé par l'app)
_last_use = 0.0       # horodatage de la dernière inférence (idle-unload)
_kernels_warm = False  # True dès que les kernels Metal ont été compilés une fois

# v1.1.0 : SUSPENSION TEMPORAIRE du GPU après un timeout d'inférence, au lieu
# d'une bascule CPU définitive. Une session Vlocal dure des semaines (l'app ne
# redémarre jamais) : condamner le GPU au premier dépassement de délai coûtait
# x8 sur toutes les dictées suivantes (mesuré : 1,0 s GPU contre 8,0 s CPU) et
# 1,5 Go de RAM. On suspend le GPU _GPU_COOLDOWN_S, puis on le retente. Seuls les
# gels de DICTÉE comptent pour la bascule (_GPU_MAX_STRIKES en _GPU_STRIKE_WINDOW_S).
_GPU_COOLDOWN_S = 600.0
_GPU_STRIKE_WINDOW_S = 3600.0
_GPU_MAX_STRIKES = 3
_gpu_suspended_until = 0.0
_gpu_suspend_reason = ""
_gpu_strikes = []

# v1.4.4 — PLUS AUCUN VERDICT NÉGATIF PERMANENT SUR UNE CAUSE PASSAGÈRE. 12 Mac
# sur 29 (dont M4 et M3 Pro) tournaient sur le CPU, ~7x plus lent, pour des
# semaines : le verdict False était mémorisé à vie alors que sa cause passait
# (modèle téléchargé APRÈS le 1er lancement, metallib « Interrupted system
# call » une seule fois, bascule après des gels). Désormais :
#   - causes DÉFINITIVES (plateforme, import MLX impossible, VLOCAL_NO_MLX) :
#     verdict gardé pour la session, aucune sonde de plus ;
#   - causes PASSAGÈRES (modèle absent, sonde Metal KO, préchauffe Metal KO) :
#     nouvel essai après 1 min, 5 min, 15 min puis toutes les 30 min. 1 min
#     rattrape l'aléa ponctuel ; le plafond de 30 min borne le coût d'une
#     machine durablement cassée à une sonde (un kernel minuscule) par demi-heure ;
#   - bascule après _GPU_MAX_STRIKES gels de dictée : au moins _FORCE_CPU_RETRY_S
#     (30 min), plus longue qu'une suspension simple (10 min) puisque le GPU a
#     déjà gelé plusieurs fois en une heure.
# Entre deux essais, available() renvoie False sans rien sonder (coût nul).
_RETRY_DELAYS_S = (60.0, 300.0, 900.0, 1800.0)
_FORCE_CPU_RETRY_S = 1800.0
_final_state = ""     # "unsupported" / "disabled" si le verdict négatif est définitif
_state_reason = ""    # raison courte du verdict négatif courant (status)
_retry_at = 0.0       # horodatage du prochain essai (verdict négatif passager)
_retry_n = 0          # essais ratés consécutifs (indexe _RETRY_DELAYS_S)

# Marqueurs d'une VRAIE erreur Metal à la préchauffe. Volontairement étroits :
# « device » seul attrapait « No space left on device » (disque plein), et
# « library » ou « gpu » n'importe quelle erreur de chargement Python.
_METAL_ERROR_MARKERS = ("metal", "command buffer", "no stream")

# v1.4.4 — LA SONDE NE TOURNE JAMAIS SANS BORNE SUR LE THREAD APPELANT. Elle
# exécute un kernel Metal : sur un GPU qui gèle, elle gèle aussi. Or available()
# est appelé par le thread qui DICTE. Règles :
#   - toute sonde tourne sur un thread dédié « mlx-probe », une seule à la fois
#     (_avail_lock + _probe_running) ;
#   - NOUVEL ESSAI : available() renvoie False tout de suite et lance la sonde en
#     fond ; le verdict vaut pour l'appel suivant (la dictée en cours part sur le
#     CPU, comme pendant tout le délai) ;
#   - 1re SONDE de la session : l'appelant attend le verdict, borné par
#     _FIRST_PROBE_TIMEOUT_S. Elle a lieu au démarrage, en fond (construction
#     du moteur) ; l'attendre évite de charger pour rien le turbo CPU (1,6 Go)
#     sur une machine dont le GPU marche. Borne large : elle couvre aussi
#     l'import à froid de mlx_whisper (plusieurs secondes sur disque froid) ;
#   - une sonde qui dépasse sa borne compte comme un échec (palier suivant).
#     Son thread, s'il reste coincé dans Metal, n'est pas doublé : tant qu'il
#     vit, chaque essai dû échoue sans relancer de kernel.
_PROBE_TIMEOUT_S = 4.0
_FIRST_PROBE_TIMEOUT_S = 20.0


def _now() -> float:
    """Horloge des délais de suspension et de nouvel essai (simulée en test)."""
    return time.time()


def _short(text: str, limit: int = 80) -> str:
    text = str(text)
    return text if len(text) <= limit else text[:limit - 1] + "…"

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

_avail_lock = _threading.RLock()   # verdict, sonde en cours, gels (réentrant :
                                    # _gpu_strike -> _force_cpu -> _set_unavailable)
_probe_running = False
_probe_done = _threading.Event()
_probe_thread = None
_probe_gen = 0                     # incrémenté par reset_availability (verdict périmé)

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


# Priorité de la sentinelle d'arrêt : APRÈS tout travail déjà en file, que
# l'ancien worker termine avant de s'arrêter (cf. renew_worker).
_PRIO_STOP = 99


def _worker_loop(jobs):
    while True:
        item = jobs.get()
        _prio, _seq, fn, args, kwargs, box, done, started = item
        if fn is None:                    # sentinelle de renew_worker
            return
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
            # v1.4.4 : seul un gel de DICTÉE compte pour la bascule (un gel de
            # réunion ou de préchauffe ne fait que suspendre).
            _gpu_strike(f"inférence MLX figée (>{_timeout:.0f}s)",
                        dictation=(_prio == PRIO_DICTATION))
            raise TimeoutError(
                "transcription MLX figée (>%.0fs) — worker réinitialisé" % _timeout)
        raise TimeoutError(
            "GPU occupé (>%.0fs) — travail en attente, GPU conservé" % _timeout)
    if "error" in box:
        raise box["error"]
    return box.get("value")


def renew_worker() -> None:
    """v1.6 — Remplace le worker MLX par un neuf au prochain appel, après une
    ERREUR (pas un gel) d'inférence. L'ancien termine les travaux déjà en file
    puis s'arrête (sentinelle) : aucun thread orphelin. Le modèle est conservé :
    mesuré, un modèle chargé sur un thread s'utilise tel quel sur un autre
    (MLX 0.31), le second essai ne paie donc aucun rechargement."""
    global _mlx_worker
    with _mlx_worker_lock:
        if _mlx_worker is None:
            return
        _mlx_jobs.put((_PRIO_STOP, _next_seq(), None, None, None, None, None, None))
        _mlx_worker = None              # _on_worker en crée un neuf, file neuve


def _base_dir():
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.dirname(os.path.abspath(__file__))


def _diag(msg: str) -> None:
    """Trace persistante du verdict GPU. L'app gelée (GUI) perd son stdout :
    sans ça, impossible de savoir si le GPU s'active vraiment. Best-effort,
    ne lève jamais. Fichier : ~/Library/Logs/Vlocal/mlx.log."""
    try:
        d = os.path.expanduser("~/Library/Logs/Vlocal")
        os.makedirs(d, exist_ok=True)
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(os.path.join(d, "mlx.log"), "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass


def _set_unavailable(reason: str, final_state: str = "",
                     min_delay: float = 0.0) -> None:
    """Pose un verdict négatif. final_state ("unsupported" / "disabled") le rend
    définitif pour la session ; sinon un nouvel essai est programmé selon
    _RETRY_DELAYS_S (au moins min_delay)."""
    global _available, _final_state, _state_reason, _retry_at, _retry_n
    with _avail_lock:
        _available = False
        _final_state = final_state
        _state_reason = reason
        if final_state:
            msg = f"MLX indisponible ({reason}) -> moteur CPU pour la session."
        else:
            delay = max(min_delay,
                        _RETRY_DELAYS_S[min(_retry_n, len(_RETRY_DELAYS_S) - 1)])
            _retry_n += 1
            _retry_at = _now() + delay
            msg = (f"MLX indisponible ({reason}) -> moteur CPU, "
                   f"nouvel essai dans {delay / 60:.0f} min.")
    print(f"[mlx] {msg}")
    _diag(msg)


def _force_cpu(reason: str, min_delay: float = 0.0) -> None:
    """v3.2.8 — Bascule sur CPU après un vrai échec Metal (préchauffe KO, gels
    répétés) : on ARRÊTE de retenter le GPU à chaque dictée (coût + latence +
    risque de re-figeage) et on rend la main au moteur CPU.
    v1.4.4 — bascule TEMPORAIRE : le GPU est re-sondé après le délai de
    _set_unavailable (au moins min_delay). Une metallib réellement cassée échoue
    à la sonde et repart pour un délai plus long ; un aléa passager se rétablit."""
    if _final_state:
        return                          # déjà hors jeu pour la session
    _set_unavailable(f"bascule CPU : {reason}", min_delay=min_delay)
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


def _gpu_strike(reason: str, *, dictation: bool = True) -> None:
    """v1.1.0 : enregistre un gel GPU. Suspend le GPU _GPU_COOLDOWN_S (le moteur
    CPU prend le relais, l'utilisateur n'attend jamais).
    v1.4.4 : seuls les gels de DICTÉE (dictation=True) entrent dans le décompte ;
    au-delà de _GPU_MAX_STRIKES dans la fenêtre glissante, bascule CPU pour
    _FORCE_CPU_RETRY_S. Un gel hors dictée (réunion, import, préchauffe sous
    charge) suspend seulement : il ne dit rien de la fiabilité de la dictée et
    faisait basculer des machines saines après une longue réunion."""
    global _gpu_suspended_until, _gpu_strikes, _gpu_suspend_reason
    with _avail_lock:
        now = _now()
        if dictation:
            _gpu_strikes = [t for t in _gpu_strikes
                            if now - t < _GPU_STRIKE_WINDOW_S] + [now]
            if len(_gpu_strikes) >= _GPU_MAX_STRIKES:
                _force_cpu(f"{reason}, {len(_gpu_strikes)} gels de dictée "
                           "en moins d'une heure", min_delay=_FORCE_CPU_RETRY_S)
                return
            tally = f"gel de dictée {len(_gpu_strikes)}/{_GPU_MAX_STRIKES}"
        else:
            tally = "gel hors dictée, non compté"
        _gpu_suspended_until = now + _GPU_COOLDOWN_S
        _gpu_suspend_reason = reason
    msg = f"GPU suspendu {_GPU_COOLDOWN_S / 60:.0f} min ({reason}), {tally}"
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
    cache local ~/.cache/huggingface). L'app peut aussi pointer _model_ref sur le
    modèle du cache (app._point_mlx_to_cache)."""
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
    # v1.4.4 : le repli dépôt HF n'est PLUS mémorisé. Mémorisé, il masquait à
    # vie un modèle bundlé arrivé après coup : la sonde le jugeait absent pour
    # toute la session. Le coût d'une nouvelle recherche est de deux stat().
    return _HF_REPO


def _metal_smoke() -> None:
    """v3.2.8 — SMOKE-TEST GPU RÉEL (pas seulement l'import). Une metallib
    incompatible (cible Metal trop récente pour ce macOS) charge à l'import mais
    ÉCHOUE au 1er kernel : un kernel minuscule force le chargement de la metallib
    + une exécution Metal. Lève si le GPU n'est pas utilisable."""
    import mlx.core as mx
    probe = float(mx.sum(mx.array([1.0, 2.0, 3.0])))
    if probe != 6.0:
        raise RuntimeError(f"probe GPU incohérent ({probe})")


def _probe() -> tuple[str, str]:
    """Sonde complète (env, plateforme, import, modèle, kernel Metal).
    Renvoie (état, raison) : "ok", "disabled" / "unsupported" (définitifs) ou
    "retry" (passager). Ne lève jamais."""
    if os.environ.get("VLOCAL_NO_MLX"):
        return "disabled", "désactivé par VLOCAL_NO_MLX"
    if sys.platform != "darwin":
        return "unsupported", f"plateforme {sys.platform}"
    try:
        import mlx.core as mx           # noqa: F401
        import mlx_whisper              # noqa: F401
    except ImportError as e:
        # Paquet absent du build : rien ne le fera apparaître pendant la session.
        return "unsupported", f"import MLX impossible ({e.name or e})"
    except Exception as e:
        # Le paquet est là mais son chargement a levé autre chose (dylib, Metal) :
        # peut être passager, on retentera.
        return "retry", f"chargement MLX KO ({type(e).__name__}: {e})"
    # En frozen, on exige le modèle bundlé (pas de téléchargement runtime,
    # promesse 100 % local). En dev, le cache HF est toléré.
    if getattr(sys, "frozen", False) and not os.path.isdir(model_ref()):
        return "retry", "modèle GPU pas encore présent"
    try:
        _metal_smoke()
    except Exception as e:
        return "retry", f"sonde Metal KO ({type(e).__name__}: {e})"
    return "ok", ""


def _run_probe_bounded(timeout: float) -> tuple[str, str]:
    """Exécute _probe sur un thread dédié et attend au plus `timeout` s."""
    global _probe_thread
    if _probe_thread is not None and _probe_thread.is_alive():
        return "retry", "sonde précédente toujours figée dans Metal"
    box = {}
    done = _threading.Event()

    def _go():
        try:
            box["verdict"] = _probe()
        finally:
            done.set()

    _probe_thread = _threading.Thread(target=_go, name="mlx-probe", daemon=True)
    _probe_thread.start()
    if not done.wait(timeout):
        return "retry", f"sonde Metal figée (>{timeout:.0f}s)"
    return box.get("verdict", ("retry", "sonde interrompue"))


def _apply_verdict(gen: int, state: str, reason: str) -> None:
    """Applique le verdict d'une sonde et libère le créneau « sonde en cours ».
    Un verdict d'avant reset_availability() est ignoré (cause peut-être levée)."""
    global _available, _retry_n, _state_reason, _probe_running
    with _avail_lock:
        _probe_running = False
        _probe_done.set()
        if gen != _probe_gen:
            return
        if state != "ok":
            _set_unavailable(reason, final_state="" if state == "retry" else state)
            return
        _available = True
        _retry_n = 0
        _state_reason = ""
    _diag(f"MLX DISPONIBLE (GPU Metal vérifié — probe OK). modèle={model_ref()}")


def _probe_and_apply(gen: int, timeout: float) -> None:
    state, reason = _run_probe_bounded(timeout)
    _apply_verdict(gen, state, reason)


def available() -> bool:
    """True si le backend GPU MLX est utilisable. Repli silencieux total :
    False -> moteur CPU. Verdict positif mis en cache ; verdict négatif
    définitif ou re-sondé après délai (cf. _RETRY_DELAYS_S). Pendant une
    suspension ou un délai d'attente, renvoie False sans rien sonder. Ne
    bloque jamais plus que la 1re sonde bornée (cf. _PROBE_TIMEOUT_S)."""
    global _gpu_suspended_until, _probe_running
    with _avail_lock:
        if _available:
            if not _gpu_suspended_until:
                return True
            if _now() < _gpu_suspended_until:
                return False
            _gpu_suspended_until = 0.0
            resumed = True
        else:
            resumed = False
            if _available is False and (_final_state or _now() < _retry_at):
                return False
            first = _available is None
            if _probe_running:
                if not first:
                    return False
                launch = False          # 1re sonde déjà partie : on attend son verdict
            else:
                _probe_running = True
                _probe_done.clear()
                launch = True
            gen = _probe_gen
            previous = _state_reason
    if resumed:
        print("[mlx] fin de suspension : GPU réactivé.")
        _diag("fin de suspension : GPU réactivé.")
        return True
    if not first:
        _diag(f"nouvel essai GPU en fond (précédent : {previous}).")
        _threading.Thread(target=_probe_and_apply, args=(gen, _PROBE_TIMEOUT_S),
                          name="mlx-probe-retry", daemon=True).start()
        return False
    if launch:
        _probe_and_apply(gen, _FIRST_PROBE_TIMEOUT_S)
    else:
        _probe_done.wait(_FIRST_PROBE_TIMEOUT_S + 1.0)
    with _avail_lock:
        return bool(_available) and not (_gpu_suspended_until
                                         and _now() < _gpu_suspended_until)


def status() -> tuple[str, str]:
    """(état, raison courte <= 80 car.) du backend GPU, pour l'affichage et la
    télémétrie. SANS EFFET DE BORD : ne sonde rien, ne charge rien, ne lève
    pas la suspension. États : "ok", "suspended", "retry", "unsupported",
    "disabled", "unknown" (jamais sondé)."""
    now = _now()
    if _available is None:
        if os.environ.get("VLOCAL_NO_MLX"):
            return "disabled", "désactivé par VLOCAL_NO_MLX"
        return "unknown", "pas encore testé"
    if _available:
        if _gpu_suspended_until and now < _gpu_suspended_until:
            left = int(_gpu_suspended_until - now)
            return "suspended", _short(f"reprise dans {left} s ({_gpu_suspend_reason})")
        return "ok", ""
    if _final_state:
        return _final_state, _short(_state_reason)
    left = max(0, int(_retry_at - now))
    return "retry", _short(f"nouvel essai dans {left} s ({_state_reason})")


def reset_availability(reason: str) -> None:
    """Oublie le verdict GPU (et le chemin du modèle) : la prochaine
    available() re-sonde immédiatement. À appeler quand la cause d'un refus
    a pu disparaître, typiquement après le téléchargement du modèle GPU.
    Un _model_ref qui désigne un modèle PRÉSENT (pointé par l'app sur le
    cache) est conservé : l'oublier ferait chercher le modèle dans le bundle
    seulement, et le refus reviendrait."""
    global _available, _final_state, _state_reason, _retry_at, _retry_n, _model_ref
    global _probe_gen
    with _avail_lock:
        if _model_ref is not None and not os.path.exists(
                os.path.join(_model_ref, "config.json")):
            _model_ref = None
        _available = None
        _final_state = ""
        _state_reason = ""
        _retry_at = 0.0
        _retry_n = 0
        _probe_gen += 1
    _diag(f"verdict GPU réinitialisé ({reason}) : nouvelle sonde au prochain usage.")


# v1.6 — ENCODEUR ACCÉLÉRÉ. L'encodeur traite TOUJOURS une fenêtre de 30 s
# (1500 positions x 32 couches), quelle que soit la durée dictée : c'est lui, et
# non le décodage, qui fait le coût fixe d'une dictée courte (mesuré sur M5 :
# ~1,2 s d'encodeur sur ~1,35 s de dictée de 5 s). Trois gains, texte inchangé
# (corpus de 17 extraits FR, sortie identique au caractère près) :
#   1. attention FUSIONNÉE (mx.fast.scaled_dot_product_attention) au lieu de
#      la matrice 20 x 1500 x 1500 explicite : encodeur -7 % (1195 -> 1108 ms) ;
#   2. évaluation COUCHE PAR COUCHE : évalué d'un bloc, le graphe paresseux des
#      32 couches gardait ~650 Mo de tampons vivants en même temps ; couche par
#      couche, le surcoût tombe à ~60 Mo (pic MLX ~1,47 -> ~0,9 Go, +5 ms) ;
#   3. MÉMO d'une entrée : la même fenêtre est ré-encodée à chaque palier du
#      repli en température et, en réunion, une seconde fois pour l'alignement
#      des mots (word_timestamps). Le mémo rend le résultat déjà calculé
#      (~1,1 s gagnée par ré-encodage évité). Vidé à la fin de chaque
#      transcription : il ne retient jamais de mémoire entre deux appels.
# Tout s'exécute sur le worker MLX : le mémo n'est jamais partagé entre threads.
_fast_encoder_cls = None
_enc_memo = None        # (mel, sortie) de la dernière fenêtre encodée


def _fused_attention(q, k, v, n_head: int):
    import mlx.core as mx
    b, n, d = q.shape
    q, k, v = (t.reshape(b, t.shape[1], n_head, -1).transpose(0, 2, 1, 3)
               for t in (q, k, v))
    o = mx.fast.scaled_dot_product_attention(q, k, v, scale=(d // n_head) ** -0.5)
    return o.transpose(0, 2, 1, 3).reshape(b, n, d)


def _encoder_class():
    """Sous-classe de l'encodeur de mlx_whisper (définie à la demande : mlx
    n'est importé qu'une fois le GPU confirmé)."""
    global _fast_encoder_cls
    if _fast_encoder_cls is None:
        import mlx.core as mx
        import mlx.nn as nn
        from mlx_whisper.whisper import AudioEncoder

        class FastAudioEncoder(AudioEncoder):
            def __call__(self, x):
                global _enc_memo
                memo = _enc_memo
                if (memo is not None and memo[0].shape == x.shape
                        and memo[0].dtype == x.dtype
                        and mx.array_equal(memo[0], x).item()):
                    return memo[1]
                h = nn.gelu(self.conv1(x))
                h = nn.gelu(self.conv2(h))
                assert h.shape[1:] == self._positional_embedding.shape, \
                    "incorrect audio shape"
                h = h + self._positional_embedding
                for blk in self.blocks:
                    a = blk.attn
                    y = blk.attn_ln(h)
                    h = h + a.out(_fused_attention(a.query(y), a.key(y),
                                                   a.value(y), a.n_head))
                    h = h + blk.mlp2(nn.gelu(blk.mlp1(blk.mlp_ln(h))))
                    mx.eval(h)
                out = self.ln_post(h)
                mx.eval(out)
                _enc_memo = (x, out)
                return out

        _fast_encoder_cls = FastAudioEncoder
    return _fast_encoder_cls


def _accelerate(model) -> None:
    """Branche l'encodeur accéléré sur un modèle mlx_whisper (idempotent)."""
    cls = _encoder_class()
    if type(model.encoder) is not cls:
        model.encoder.__class__ = cls


def _forget_encoding() -> None:
    global _enc_memo
    _enc_memo = None


def _ensure_model():
    """Charge (ou retrouve) le modèle GPU, encodeur accéléré branché.
    Worker MLX uniquement. mlx_whisper.transcribe retrouve ensuite ce même
    objet dans ModelHolder (même chemin, même type)."""
    import mlx.core as mx
    from mlx_whisper.transcribe import ModelHolder
    model = ModelHolder.get_model(model_ref(), mx.float16)
    _accelerate(model)
    return model


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
            _ensure_model()
            buf = (np.random.randn(8000).astype(np.float32) * 0.01)
            try:
                mlx_whisper.transcribe(buf, path_or_hf_repo=model_ref(),
                                       language="fr", fp16=True)
            finally:
                _forget_encoding()
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
            _ensure_model()
            print("[mlx] modèle GPU rechargé (poids seuls).")
    except Exception as e:
        print(f"[mlx] préchauffe KO (sans gravité) : {e}")
        # v3.2.8 — si l'échec est une VRAIE erreur Metal (GPU/metallib cassé), on ne
        # retente pas le GPU à chaque dictée : bascule CPU (temporaire depuis v1.4.4).
        # Toute autre erreur (disque plein, poids illisibles) n'accuse pas le GPU.
        _es = str(e).lower()
        if any(k in _es for k in _METAL_ERROR_MARKERS):
            _force_cpu(f"préchauffe Metal KO ({type(e).__name__})")
        else:
            _diag(f"préchauffe KO hors Metal, GPU conservé ({type(e).__name__}: {e})")
    finally:
        _last_use = time.time()


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
        _ensure_model()
        return mlx_whisper.transcribe(
            a, path_or_hf_repo=model_ref(), language=language,
            condition_on_previous_text=False,
            initial_prompt=(initial_prompt or None),
            word_timestamps=word_timestamps, fp16=True, **kw)
    finally:
        _last_use = time.time()
        _forget_encoding()
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
    return (time.time() - _last_use) if (loaded() and _last_use) else 0.0


def loaded() -> bool:
    """True si le modèle MLX est actuellement RÉSIDENT en mémoire GPU."""
    try:
        from mlx_whisper.transcribe import ModelHolder
        return ModelHolder.model is not None
    except Exception:
        return False


def _unload_impl() -> None:
    _forget_encoding()
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
