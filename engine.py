#!/usr/bin/env python3
"""
Vlocal — Moteur de transcription (Whisper large-v3-turbo int8 / faster-whisper)

v15 — VIRAGE PRODUIT : Vlocal devient le meilleur transcripteur local FR, point.
Plus aucune IA générative (SLM/Ollama supprimés). Tout est déterministe.

VlocalEngine charge le modèle UNE SEULE FOIS et le garde chaud. 100% local, CPU.

Optimisations Whisper actives (cf. brief v15) :
  1. VAD Silero (vad_filter=True + paramètres) — coupe les silences, tue les
     hallucinations "Amara.org" sur les blancs.
  2. hotwords dynamiques construits depuis le glossaire utilisateur — biaisent
     le décodeur vers les noms propres/jargon (gain massif de fidélité).
  3. Confidence par mot (word_timestamps=True) — exploité en RÉUNION.
  4. Greedy beam_size=1 partout (mesuré : beam 5 = +30 % de temps, zéro gain
     sur turbo) + filet de température natif.
  5. Chunking long-format géré nativement par faster-whisper + VAD.
  6. Anti-hallucinations renforcé (liste noire regex + dé-duplication des
     répétitions en boucle).
"""

import os
import re
import threading

import numpy as np

import mlx_engine   # backend GPU optionnel (repli CPU auto si indisponible)


# --------------------------------------------------------------------------- #
# v1.0.9 — AUTO-HEAL AUDIO : bornes anti-figeage PortAudio / CoreAudio.
# Un changement de périphérique (Bluetooth, casque, dock) peut FIGER stop() /
# close() / open() du flux micro. Sans borne, l'appelant — et tout verrou qu'il
# tient — resterait coincé À VIE : la dictée « cale » et ne peut plus redémarrer.
# Ces helpers exécutent l'opération sur un thread daemon et ABANDONNENT si elle
# dépasse le délai : l'app garde toujours la main. L'audio déjà capturé est en
# mémoire (self._frames), indépendant du flux -> jamais perdu.
# --------------------------------------------------------------------------- #
def _close_stream_bounded(stream, timeout: float = 3.0) -> None:
    """Ferme un flux (stop+close) sans jamais bloquer l'appelant > timeout."""
    if stream is None:
        return
    done = threading.Event()

    def _close():
        try:
            try:
                stream.stop()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
        finally:
            done.set()
    threading.Thread(target=_close, name="audio-close", daemon=True).start()
    if not done.wait(timeout):
        try:
            print(f"[audio] fermeture du flux figée (>{timeout:.0f}s) -> abandonnée (auto-heal).")
        except Exception:
            pass


def _open_input_stream_bounded(make_stream, timeout: float = 4.0):
    """Crée + démarre un InputStream sous borne. Retourne le flux si OK ; lève
    TimeoutError si l'ouverture fige (>timeout) ou propage l'exception de
    création. Si l'ouverture abandonnée finit par aboutir, on referme le flux
    orphelin (sinon il tiendrait le micro)."""
    box = {}
    done = threading.Event()
    abandoned = threading.Event()

    def _open():
        try:
            s = make_stream()
            s.start()
            if abandoned.is_set():
                _close_stream_bounded(s, 2.0)   # trop tard : ne pas tenir le micro
            else:
                box["stream"] = s
        except Exception as e:
            box["exc"] = e
        finally:
            done.set()
    threading.Thread(target=_open, name="audio-open", daemon=True).start()
    if not done.wait(timeout):
        abandoned.set()
        raise TimeoutError(f"ouverture du flux micro figée (>{timeout:.0f}s)")
    if "exc" in box:
        raise box["exc"]
    return box.get("stream")


def _call_bounded(fn, timeout: float, default=None):
    """v1.0.22 — Exécute `fn()` sous BORNE et renvoie `default` si l'appel fige
    ou lève. Généralise le patron _close_stream_bounded aux appels PortAudio
    d'ÉNUMÉRATION (query_devices), non bornés jusqu'ici alors que ce sont eux
    qui figent quand CoreAudio est coincé. Le thread abandonné est daemon : il
    ne retient jamais l'appelant ni l'extinction de l'app."""
    box = {}
    done = threading.Event()

    def _go():
        try:
            box["v"] = fn()
        except Exception as e:
            box["exc"] = e
        finally:
            done.set()
    threading.Thread(target=_go, name="audio-call", daemon=True).start()
    if not done.wait(timeout):
        return default
    return box.get("v", default)


def _reinit_portaudio_bounded(timeout: float = 3.0) -> bool:
    """v1.0.11 — AUTO-HEAL PROFOND : ré-initialise PortAudio (terminate + initialize)
    SOUS BORNE. Récupère un sous-système CoreAudio/PortAudio COINCÉ (le micro refuse
    de se rouvrir après un flux mal relâché par l'OS, une bascule de périphérique,
    etc.) — c'est ce qu'un redémarrage de l'app faisait, ici SANS redémarrer.
    BEST-EFFORT STRICT : toute erreur est avalée (zéro régression — l'appelant reste
    exactement dans l'état d'avant). À n'appeler QUE flux fermé (aucun stream ouvert).
    Retourne True si la ré-init a abouti dans le délai (sinon abandonnée, sans effet)."""
    done = threading.Event()
    ok = {"v": False}

    def _go():
        try:
            import sounddevice as _sd
            try:
                _sd._terminate()
            except Exception:
                pass
            try:
                _sd._initialize()
            except Exception:
                pass
            # v1.0.13 — ANTI « Error querying device -1 » : si, après ré-init, le
            # device d'entrée par défaut est invalide (PortAudio à -1, typiquement
            # après le débranchement d'un périphérique / bascule audio), on CHERCHE
            # un micro VALIDE et on le force comme défaut -> le start_recording
            # suivant l'ouvre. Vraie auto-réparation : plus besoin de redémarrer
            # l'app. Best-effort strict (zéro exception remontée).
            try:
                _sd.query_devices(kind="input")          # défaut OK -> on ne touche à rien
            except Exception:
                try:
                    _pick = None
                    for _i, _d in enumerate(_sd.query_devices()):
                        if int(_d.get("max_input_channels", 0)) >= 1:
                            _pick = _i
                            if "macbook" in str(_d.get("name", "")).lower():
                                break          # préfère le micro intégré
                    if _pick is not None:
                        _sd.default.device = (_pick, None)
                except Exception:
                    pass
            ok["v"] = True
        except Exception:
            pass
        finally:
            done.set()
    threading.Thread(target=_go, name="audio-reinit", daemon=True).start()
    if not done.wait(timeout):
        try:
            print(f"[audio] ré-init PortAudio figée (>{timeout:.0f}s) -> abandonnée (auto-heal).")
        except Exception:
            pass
        return False
    return ok["v"]


# v1.0.22 — MICRO BÉTON : la capture de DICTÉE passe par un PROCESS JETABLE
# (micworker). Terrain 1.0.21 : 28 échecs micro, 0 récupération — l'auto-heal
# in-process ne peut pas gagner quand PortAudio est corrompu dans CE process
# (threads de close abandonnés coincés dedans) ; seul un process NEUF garantit
# un état CoreAudio neuf (c'est pourquoi « relancer l'app » marchait toujours).
# VLOCAL_MIC_PROC=0 = retour intégral au chemin in-process historique.
_MIC_PROC_ENABLED = os.environ.get("VLOCAL_MIC_PROC", "1") != "0"
try:
    import micworker as _micworker
except Exception:
    _micworker = None

# Modèle principal : turbo int8 ; small int8 chargé à la demande (cascade/dictée rapide).
# Noms de dossiers CANONIQUES (point de vérité unique pour engine.py et app.py).
TURBO_DIR_NAME = "whisper-large-v3-turbo-int8"
SMALL_DIR_NAME = "whisper-small-int8"
MODEL_DIR_DEFAULT = "models/" + TURBO_DIR_NAME
SAMPLE_RATE = 16000

# Fenêtrage de la transcription incrémentale (import de longs WAV) : on traite
# par fenêtres ~25 s coupées sur un silence, avec pacing -> charge CPU lissée.
WINDOW_TARGET_S = 25.0
WINDOW_MAX_S = 40.0
SILENCE_RMS = 0.012

# v1.1.0 : plafond de l'inférence GPU d'une DICTÉE, proportionnel à l'audio.
# Un plafond fixe (22 s) déclarait « gel Metal » une transcription légitime dès
# que le reliquat dépassait ~4 min (RTF turbo ~0,05, jusqu'à ~0,3 avec les
# re-décodages en température sur audio difficile). 0,5 s par seconde d'audio
# reste très au-dessus du légitime et borne quand même tout vrai figeage.
DICT_GPU_TIMEOUT_BASE_S = 22.0
DICT_GPU_TIMEOUT_PER_AUDIO_S = 0.5


def dictation_gpu_timeout(audio_s: float) -> float:
    """Délai maximal accordé à l'inférence GPU d'une dictée de `audio_s` secondes."""
    return DICT_GPU_TIMEOUT_BASE_S + DICT_GPU_TIMEOUT_PER_AUDIO_S * max(0.0, float(audio_s))
SILENCE_SEARCH_S = 8.0   # fenêtre de recherche d'un silence après la cible

# Garde-fous anti-hallucination sur silence/bruit faible (RMS gate amont).
PEAK_GATE = 0.01
RMS_GATE = 0.003


_QOS_WARNED = False


def _qos_user_initiated() -> None:
    """v21 — Place le thread APPELANT en QOS_CLASS_USER_INITIATED (0x19).
    Sur Apple Silicon, la QoS détermine le placement P-core vs E-core : un
    thread démoté (BACKGROUND 0x09) est CONFINÉ aux E-cores ~1 GHz -> latence
    2-3x, aléatoire. NB (vérifié, source libpthread Apple) : les workers du
    pool CTranslate2 n'héritent PAS de cette QoS (pthread_create -> DEFAULT
    0x15, déjà éligible P-cores) — l'appel protège le thread PYTHON (GIL,
    pré/post-traitement, itération du générateur). Best-effort, no-op hors
    macOS ; EPERM loggé une fois (thread opté-out du système QoS)."""
    global _QOS_WARNED
    try:
        import ctypes
        err = ctypes.CDLL(None).pthread_set_qos_class_self_np(0x19, 0)
        if err and not _QOS_WARNED:
            _QOS_WARNED = True
            print(f"[qos] pthread_set_qos_class_self_np a renvoyé {err} "
                  "(thread hors système QoS — placement P-core non garanti).")
    except Exception:
        pass

# v15 — Liste noire d'hallucinations Whisper (génériques d'entraînement qui
# remontent sur les silences/musiques). Regex insensibles à la casse.
_HALLUCINATION_PATTERNS = [
    r"sous[- ]titres?\s+r[ée]alis[ée]s?\s+par.*",
    r"sous[- ]titrage\s+(?:mfp|: soci[ée]t[ée] radio[- ]canada).*",
    r"sous[- ]titres?\s*:\s*.*",
    r"merci\s+d['’]avoir\s+regard[ée].*",
    r"thanks?\s+for\s+watching.*",
    r"thank\s+you\s+for\s+watching.*",
    r"subtitles?\s+by.*",
    r"soustitreur\.com",
    r"amara\.org",
    r"communaut[ée]\s+d?['’]?amara",
    r"\[?\s*musique\s*\]?",
    r"\[?\s*applaudissements\s*\]?",
    r"\[?\s*rires\s*\]?",
    r"♪+",
    r"www\.\S+\.\w+",
]
_HALLUCINATION_RE = [re.compile(p, re.IGNORECASE) for p in _HALLUCINATION_PATTERNS]

# Fragments encore utilisés par l'ancien garde « sortie ENTIÈREMENT hallucinée ».
_HALLUCINATION_FRAGMENTS = [
    "amara.org", "communauté d'amara", "communauté amara", "sous-titrage mfp",
    "sous-titrage : société radio-canada", "soustitreur.com",
    "merci d'avoir regardé", "thanks for watching", "thank you for watching",
    "subtitles by", "subtitle by",
]


def _is_known_hallucination(text: str) -> bool:
    """True si un FRAGMENT d'hallucination connu subsiste dans la sortie
    (test par sous-chaîne — PAS « sortie entièrement hallucinée »).

    Filet quasi mort en pratique : les call-sites l'appliquent à un texte déjà
    passé par strip_hallucinations, dont les regex couvrent chacun de ces
    fragments. Seul cas résiduel : le recollage par la passe ponctuation
    `\\s+([,.;:!?…])` de strip_hallucinations (ex. « amara [musique].org » ->
    « amara.org »). ATTENTION : un déclenchement jette TOUT le texte ('')."""
    if not text:
        return False
    t = text.strip().lower()
    return any(frag in t for frag in _HALLUCINATION_FRAGMENTS)


def collapse_repetition_loops(text: str, max_run: int = 8) -> str:
    """v28 — BOUCLES DE RÉPÉTITION (« c'est c'est c'est ... » x60) : un même
    token répété > max_run fois d'affilée est tronqué à 2 occurrences. Filet
    post-hoc réunion (le fallback température de Whisper en laisse passer de
    courtes). Pur ; conserve la ponctuation/espaces standards."""
    import re as _re
    def _cut(m):
        return (m.group(1) + " ") * 2
    return _re.sub(r"\b([\w'’]{1,14})\b(?:[,\s]+\1\b){%d,}[,\s]*" % max_run,
                   _cut, text)


def strip_hallucinations(text: str) -> str:
    """v15 — Nettoyage anti-hallucination de la SORTIE Whisper :
      1. retire les motifs de la liste noire (génériques de sous-titres, etc.)
      2. dé-duplique une phrase de >5 mots répétée 2x+ verbatim (boucle Whisper)
    Conserve tout le reste tel quel (déterministe, sans rien inventer)."""
    if not text:
        return text or ""
    out = text
    for rx in _HALLUCINATION_RE:
        out = rx.sub(" ", out)
    # Dé-duplication des phrases longues répétées (signe de boucle).
    sentences = re.split(r"(?<=[.!?])\s+", out)
    seen, kept = set(), []
    for s in sentences:
        norm = re.sub(r"\s+", " ", s.strip().lower())
        nwords = len(norm.split())
        if nwords > 5 and norm in seen:
            continue  # boucle : on saute les répétitions
        if nwords > 5:
            seen.add(norm)
        kept.append(s)
    out = " ".join(kept)
    out = re.sub(r"\s+([,.;:!?…])", r"\1", out)
    out = re.sub(r"\s{2,}", " ", out).strip()
    return out


# v11 — Glossaire personnel (biais lexical Whisper ; v20 : hotwords).
# Une entrée par ligne dans ~/Library/Application Support/Vlocal/glossary.txt
# Exemples :
#   Lemaire
#   Dr Putcrabey
#   Amlor
#   Oreegami
#   Captiv
# Chargé à chaque appel transcribe() (cache TTL court).
import time as _time

_GLOSSARY_PATH = os.path.expanduser(
    "~/Library/Application Support/Vlocal/glossary.txt"
)
_GLOSSARY_CACHE = {"hotwords": "", "mtime": 0.0, "checked_at": 0.0}

# v15 — Termes "corrects" du glossaire de corrections (table SQLite). app.py
# appelle set_correction_terms() au démarrage et à chaque modif du glossaire.
# Ces termes enrichissent les hotwords Whisper (biais vers la bonne forme).
_CORRECTION_TERMS: list = []
_CORRECTION_LOCK = threading.Lock()


def set_correction_terms(terms) -> None:
    """Met à jour la liste des formes correctes à biaiser dans Whisper.
    `terms` : itérable de chaînes (les formes cibles, ex. 'Captiv', 'Neolife')."""
    global _CORRECTION_TERMS
    with _CORRECTION_LOCK:
        seen, clean = set(), []
        for t in (terms or []):
            t = (t or "").strip()
            k = t.lower()
            if t and k not in seen:
                seen.add(k)
                clean.append(t)
        _CORRECTION_TERMS = clean
        # invalide le cache pour reconstruire les hotwords (sous le MÊME
        # verrou : l'invalidation est vue avant la prochaine inférence)
        _GLOSSARY_CACHE["checked_at"] = 0.0
        _GLOSSARY_CACHE["mtime"] = -1.0


def invalidate_glossary_cache() -> None:
    """Invalide le cache du prompt glossaire SOUS VERROU. À appeler depuis
    app.py (plutôt que de muter _GLOSSARY_CACHE directement) pour que
    l'invalidation soit visible des threads d'inférence."""
    with _CORRECTION_LOCK:
        _GLOSSARY_CACHE["checked_at"] = 0.0
        _GLOSSARY_CACHE["mtime"] = -1.0


def _glossary_txt_terms() -> list:
    """Termes du glossaire Whisper texte (~/.../glossary.txt)."""
    try:
        if not os.path.exists(_GLOSSARY_PATH):
            return []
        with open(_GLOSSARY_PATH, "r", encoding="utf-8") as f:
            return [ln.strip() for ln in f if ln.strip()
                    and not ln.strip().startswith("#")]
    except Exception:
        return []


def build_hotwords(terms) -> str:
    """v20 — Construit la chaîne `hotwords` faster-whisper depuis les termes du
    glossaire. REMPLACE l'initial_prompt « phrase FR bornée à 220 caractères »
    (R&D v20, mesuré sur corpus) :
      - hotwords est l'API DÉDIÉE au biais lexical, non soumise au plafond du
        prompt -> plus AUCUN terme silencieusement perdu sur gros glossaire
        (avant : break au 1er dépassement, les termes en fin de liste — souvent
        les plus récents — n'avaient aucun effet) ;
      - mesure A/B (corpus FR, gros glossaire 40 termes) : WER 0,059 -> 0,000
        sur clip court (« Neolife » récupéré), 0,034 -> 0,021 avec 2/2 termes
        sur dictée longue ; prompt+hotwords ENSEMBLE moins bon que hotwords
        seuls (interférence) -> hotwords SEULS, plus d'initial_prompt ;
      - innocuité vérifiée en réunion (WER 0,0293 vs 0,0307 sans).
    Garde-fou : borné à ~380 caractères (~40 termes). MESURÉ : le biais se
    DILUE quand la liste grossit — à ~38 termes le rappel des noms propres
    reste parfait sur clip court (WER 0,000), à ~65 termes il se perd. 380 car
    = le régime efficace, et déjà 2x la couverture de l'ancien prompt. Coupe à
    la frontière d'un terme — l'appelant met les PRIORITAIRES (corrections
    actives) en tête."""
    seen, clean = set(), []
    for t in (terms or []):
        t = (t or "").strip()
        k = t.lower()
        if t and k not in seen:
            seen.add(k)
            clean.append(t)
    if not clean:
        return ""
    out = ""
    for t in clean:
        add = ("" if not out else ", ") + t
        if len(out) + len(add) > 380:
            break
        out += add
    return out


def _load_glossary_hotwords() -> str:
    """hotwords Whisper = termes de corrections (PRIORITAIRES) + glossaire
    texte. Cache léger (revérifié max toutes les 2 s). TOUT l'accès au cache est
    sous _CORRECTION_LOCK : une invalidation cross-thread (set_correction_terms,
    invalidate_glossary_cache) est vue avant la prochaine inférence.
    NB : pas de `with _CORRECTION_LOCK` imbriqué ici (verrou NON réentrant) —
    la lecture de _CORRECTION_TERMS est couverte par le verrou externe."""
    now = _time.time()
    with _CORRECTION_LOCK:
        if now - _GLOSSARY_CACHE["checked_at"] < 2.0:
            return _GLOSSARY_CACHE["hotwords"]
        _GLOSSARY_CACHE["checked_at"] = now
        try:
            mtime = os.path.getmtime(_GLOSSARY_PATH) if os.path.exists(_GLOSSARY_PATH) else 0.0
            if mtime == _GLOSSARY_CACHE["mtime"]:
                return _GLOSSARY_CACHE["hotwords"]
            corr = list(_CORRECTION_TERMS)
            terms = corr + _glossary_txt_terms()
            hot = build_hotwords(terms)
            _GLOSSARY_CACHE["hotwords"] = hot
            _GLOSSARY_CACHE["mtime"] = mtime
            return hot
        except Exception:
            return ""


def _audio_stats(audio: "np.ndarray") -> tuple:
    """Renvoie (rms, peak) du buffer float32, robuste aux empty/NaN."""
    if audio is None or len(audio) == 0:
        return 0.0, 0.0
    a = audio.astype(np.float32, copy=False)
    peak = float(np.max(np.abs(a))) if a.size else 0.0
    rms = float(np.sqrt(np.mean(a * a))) if a.size else 0.0
    return rms, peak


def _is_silent(rms: float, peak: float) -> bool:
    """Gate amont anti-hallucination : True si le buffer est jugé silencieux.
    Prédicat UNIQUE partagé par les 4 chemins de transcription. Signature
    (rms, peak) dans l'ordre du dépaquetage de _audio_stats."""
    return peak < PEAK_GATE or rms < RMS_GATE


# Ponctuation qui ne doit JAMAIS être précédée d'une espace (français simple).
# (re.escape pour neutraliser ] ) . etc. à l'intérieur de la classe de caractères.)
_NO_SPACE_BEFORE = ",.;:!?…)]»%"
_OPENERS = "([«"
_NO_SPACE_RE = re.compile(r"\s+([" + re.escape(_NO_SPACE_BEFORE) + r"])")
_OPENER_RE = re.compile(r"([" + re.escape(_OPENERS) + r"])\s+")


def assemble_segments(segment_texts) -> str:
    """Assemble proprement les textes de segments Whisper en français correct.

    Robuste aux DEUX cas observés selon le découpage :
      - segments avec espace de tête (" Bonjour", " Comment...")
      - segments SANS espace de tête ("bonjour", "salut")  -> cause du bug
        historique de mots collés ("bonjoursalut").

    On strip chaque segment puis on joint avec une espace unique, et on
    supprime les espaces parasites avant la ponctuation.
    """
    parts = [t.strip() for t in segment_texts if t and t.strip()]
    text = " ".join(parts)
    text = re.sub(r"\s+", " ", text)        # espaces multiples -> une
    text = _NO_SPACE_RE.sub(r"\1", text)    # pas d'espace avant ,.;:!?…)]»%
    text = _OPENER_RE.sub(r"\1", text)      # pas d'espace après ([«
    return text.strip()


# v15 — Paramètres VAD Silero (commun à tous les modes).
_VAD_PARAMS = dict(min_silence_duration_ms=500, threshold=0.5)

# v2 — FILET DE TEMPÉRATURE (fallback natif Whisper). Greedy à 0.0 d'abord ;
# si un segment échoue aux seuils qualité (compression_ratio / log_prob), Whisper
# le RE-DÉCODE à température croissante pour le rattraper au lieu de cracher du
# charabia. MESURÉ : coût NUL sur audio propre (ne se déclenche que sur segment
# raté), gain net de robustesse. Le désactiver (0.0 seul) retirait ce filet.
_TEMP_FALLBACK = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)

# v2 — Options d'inférence par mode. beam_size=1 PARTOUT : mesuré, beam 5
# n'apporte AUCUN gain de fidélité sur turbo (le vrai levier est le glossaire/
# hotwords) mais coûte +30% de temps. On garde donc la vitesse maximale
# AVEC le filet de température pour la robustesse.
#   dictee  : rapide, greedy + filet. Pas de word_timestamps (latence min).
#   reunion : idem + word_timestamps=True (confiance par mot pour l'affichage).
_MODE_OPTS = {
    "dictee":  dict(beam_size=1, best_of=1, temperature=_TEMP_FALLBACK,
                    word_timestamps=False),
    "reunion": dict(beam_size=1, best_of=1, temperature=_TEMP_FALLBACK,
                    word_timestamps=True),
}


def _mode_opts(mode: str) -> dict:
    return dict(_MODE_OPTS.get((mode or "dictee").lower(), _MODE_OPTS["dictee"]))


def _common_whisper_kwargs(with_thresholds: bool = True) -> dict:
    """Socle des kwargs Whisper PARTAGÉ par _base_kwargs et _small_with_conf.
    Retourne un dict NEUF à chaque appel (copie fraîche des paramètres VAD) :
    les appelants le complètent (mode, glossaire) sans fuite d'état d'un appel
    sur l'autre (un dict partagé ferait fuir les hotwords d'un appel
    précédent dans la passe small « SANS biais glossaire »).

    with_thresholds=False : passe small de la cascade (_small_with_conf), qui
    décode SANS les seuils explicites (défauts de la lib, comme avant)."""
    kwargs = {
        "language": "fr",
        "vad_filter": True,
        "vad_parameters": dict(_VAD_PARAMS),
        "condition_on_previous_text": False,
    }
    if with_thresholds:
        # Seuils qualité/anti-hallucination EXPLICITES (audit v18 AUD-09) :
        # ce sont les valeurs par défaut de faster-whisper, mais on les fixe
        # ici pour (1) verrouiller le comportement contre une dérive lors
        # d'une montée de version, (2) tracer le réglage pour un audit RGPD.
        #   no_speech_threshold : au-delà, le segment est jugé "sans parole".
        #   log_prob_threshold  : en-deçà (moyenne logprob), segment rejeté.
        #   compression_ratio_threshold : au-delà, sortie jugée répétitive.
        kwargs["no_speech_threshold"] = 0.6
        kwargs["log_prob_threshold"] = -1.0
        kwargs["compression_ratio_threshold"] = 2.4
    return kwargs


class VlocalEngine:
    def __init__(self, model_dir: str = MODEL_DIR_DEFAULT, sample_rate: int = SAMPLE_RATE,
                 cpu_threads: int = 4):
        from faster_whisper import WhisperModel

        # v21 — QoS USER_INITIATED sur le thread CONSTRUCTEUR : le pool de
        # threads CTranslate2 créé par WhisperModel() en hérite. Sans ça, le
        # chargement en thread d'arrière-plan léguait une QoS basse à TOUT le
        # pool d'inférence -> placement E-cores possible = latence aléatoire.
        _qos_user_initiated()
        self.model_dir = model_dir
        self.sample_rate = sample_rate
        # Garde-fou : sans dossier modèle, WhisperModel tenterait un
        # téléchargement HuggingFace au runtime (ou échouerait avec une erreur
        # HF cryptique). Échec LOCAL propre, avant tout réseau.
        if not os.path.exists(os.path.join(model_dir, "model.bin")):
            raise RuntimeError(f"Modèle Whisper introuvable : {model_dir}")
        # v23 — RAM 8 Go : si le GPU MLX est le moteur principal, on NE charge
        # PAS le turbo CPU au démarrage (1,7 Go inutiles). Le CPU n'est qu'un
        # REPLI -> chargé PARESSEUSEMENT (_ensure_model) seulement si MLX échoue.
        # Sinon (Intel/Windows/MLX absent) : chargé tout de suite comme avant.
        if mlx_engine.available():
            self.model = None
            print("[whisper] moteur GPU MLX actif -> turbo CPU en repli "
                  "paresseux (RAM économisée).")
        else:
            self.model = WhisperModel(model_dir, device="cpu",
                                      compute_type="int8", cpu_threads=cpu_threads)
        # v7 — diagnostic capture (chiffres de la dernière dictée)
        self.last_rms: float = 0.0
        self.last_peak: float = 0.0
        self.last_device_name: str = "?"
        self.last_model: str | None = None   # branche de la dernière dictée (small/turbo)

        self._frames = []
        self._lock = threading.Lock()
        # v1.0.22 — worker micro (process jetable) : client persistant (chaud),
        # micro fermé au repos. _rec_via_worker = la dictée EN COURS passe par lui.
        self._remote_mic = None
        self._rec_via_worker = False
        # v15 — faster-whisper WhisperModel n'est PAS thread-safe : deux appels
        # transcribe() concurrents (ex. dictée pendant qu'une réunion se
        # transcrit en fond) peuvent corrompre l'état CTranslate2 ou crasher.
        # On sérialise TOUTE inférence Whisper avec ce verrou dédié.
        self._infer_lock = threading.Lock()
        # Verrou dédié au chargement paresseux de small : empêche deux appels
        # concurrents de _ensure_fast_model() de l'instancier en double (double
        # allocation RAM). Séparé de _infer_lock pour ne pas bloquer l'inférence
        # de turbo pendant le chargement de small.
        self._fast_model_lock = threading.Lock()
        # Vlocal 2 — modèle « rapide » (small) chargé À LA DEMANDE pour la dictée
        # rapide / la cascade. Reste None tant qu'on n'en a pas besoin.
        # small ~0,5 Go : assez léger pour être chargé sur toute machine (8 Go
        # inclus) ; c'est turbo (1,7 Go) qui est le poids lourd. On charge donc
        # small à la demande, sans gating RAM.
        self._fast_model = None
        self._fast_last_use = 0.0   # dernier usage de small (idle-unload séparé)
        # v20 — pipeline BATCHED (import de fichiers UNIQUEMENT) construit
        # paresseusement autour de turbo. Enveloppe légère : ~0 RAM propre, le
        # pic vient du batch décodé (cf. transcribe_file_batched).
        self._batched = None
        self._cpu_threads = cpu_threads
        self.last_use = _time.time()   # horodatage d'activité (déchargement idle)
        self._stream = None
        self._recording = False

        self._log_input_device()

    # ------------------------------------------------------------------ #
    # v18 — Déchargement/rechargement contrôlé pour le RELAIS RAM avec la
    # diarisation. NB : avec le backend LÉGER sherpa-onnx (~150 Mo de RAM au
    # pic), le relais n'est PAS strictement nécessaire (turbo 1,7 Go +
    # diarisation ~0,15 Go = ~1,9 Go < cible 2,5 Go) ; ces méthodes restent
    # fournies pour les machines très contraintes (toggle d'option côté app).
    def unload_model(self) -> None:
        """Libère turbo (et small) de la RAM. Sérialisé avec l'inférence."""
        with self._infer_lock:
            self.model = None
            self._fast_model = None
            self._batched = None   # v20 — enveloppe liée à l'instance turbo
        import gc
        gc.collect()
        print("[whisper] modèle déchargé (relais RAM).")

    def unload_fast_model(self) -> None:
        """Libère SEUL le modèle small (turbo reste chargé) -> rend ~480 Mo quand
        small n'a pas servi depuis un moment. Rechargé paresseusement au besoin
        (_ensure_fast_model). Sérialisé avec l'inférence."""
        with self._infer_lock:
            if self._fast_model is None:
                return
            self._fast_model = None
        import gc
        gc.collect()
        print("[whisper] modèle rapide (small) déchargé (RAM rendue).")

    def load_model(self):
        """Recharge turbo en mémoire après un unload_model(). Idempotent.
        Renvoie l'instance chargée (référence locale anti-course pour les
        appelants)."""
        _qos_user_initiated()   # v21 — le pool CT2 recréé hérite de la QoS
        with self._infer_lock:
            if self.model is not None:
                return self.model
            # Garde-fou : sans dossier modèle, WhisperModel tenterait un
            # téléchargement HuggingFace au runtime (ou échouerait avec une
            # erreur HF cryptique). Échec LOCAL propre, avant tout réseau.
            if not os.path.exists(os.path.join(self.model_dir, "model.bin")):
                raise RuntimeError(
                    f"Modèle Whisper introuvable : {self.model_dir}")
            from faster_whisper import WhisperModel
            self.model = WhisperModel(self.model_dir, device="cpu",
                                      compute_type="int8",
                                      cpu_threads=self._cpu_threads)
            m = self.model
        print("[whisper] modèle rechargé.")
        return m

    def _ensure_model(self):
        """Chargement PARESSEUX : recharge turbo s'il a été déchargé (économie
        RAM à l'inactivité). Appelé en tête de chaque inférence -> on peut
        décharger librement quand l'app est inactive. Marque l'activité.
        Renvoie la RÉFÉRENCE LOCALE au modèle : une fois capturée non-None, un
        unload_model() concurrent ne peut plus l'invalider sous l'appelant."""
        self.last_use = _time.time()
        m = self.model
        if m is None:
            m = self.load_model()
        return m

    def _ensure_fast_model(self):
        """Charge le modèle small (rapide, ~0,5 Go, ~15x temps réel) à la
        demande. small est le moteur de la dictée rapide et de la passe 1 de la
        cascade auto (les réunions live restent sur TURBO, cf.
        transcribe_window) ; il est assez léger pour être chargé sur toute
        machine (8 Go inclus). Renvoie l'INSTANCE small (référence locale
        anti-course avec unload_fast_model, truthy) ou False si indisponible."""
        self._fast_last_use = _time.time()   # marque l'activité small (idle-unload)
        fm = self._fast_model
        if fm is not None:
            return fm
        _qos_user_initiated()   # v21 — le pool CT2 de small hérite de la QoS
        base = os.path.dirname(self.model_dir) if os.path.dirname(self.model_dir) else "models"
        small = os.path.join(base, SMALL_DIR_NAME)
        if not os.path.exists(os.path.join(small, "model.bin")):
            return False
        with self._fast_model_lock:
            fm = self._fast_model
            if fm is not None:   # double-vérification sous verrou
                return fm
            try:
                from faster_whisper import WhisperModel
                fm = WhisperModel(
                    small, device="cpu", compute_type="int8",
                    cpu_threads=self._cpu_threads)
                self._fast_model = fm
                print("[whisper] modèle rapide (small) chargé pour la dictée rapide.")
                return fm
            except Exception as e:
                print(f"[whisper] modèle rapide indisponible : {e}")
                return False

    # ------------------------------------------------------------------ #
    def warmup(self):
        """Préchauffe CTranslate2 : la TOUTE PREMIÈRE inférence d'un modèle est
        nettement plus lente (allocation des buffers + JIT). On lance une
        mini-inférence À BLANC (0,5 s de bruit faible, VAD désactivé) sur les
        modèles déjà chargés -> la 1re dictée RÉELLE est aussi rapide que les
        suivantes. N'altère RIEN du pipeline (buffer jetable, résultat ignoré).

        Chaque inférence est SÉRIALISÉE par _infer_lock (WhisperModel n'est pas
        thread-safe et une dictée réelle peut démarrer pendant la préchauffe).
        Verrou pris PAR modèle : une dictée concurrente n'attend au pire qu'une
        seule inférence de préchauffe.

        v23 — préchauffe AUSSI le GPU MLX s'il est dispo (compile Metal ~1,2 s)."""
        try:
            mlx_engine.warmup()
        except Exception:
            pass
        buf = (np.random.randn(SAMPLE_RATE // 2).astype(np.float32) * 0.02)
        for tag, attr in (("turbo", "model"), ("small", "_fast_model")):
            with self._infer_lock:
                # Relit l'attribut SOUS le verrou : honore un unload concurrent.
                m = getattr(self, attr)
                if m is None:
                    continue
                try:
                    t0 = _time.time()
                    segs, _info = m.transcribe(buf, beam_size=1,
                                               vad_filter=False, language="fr")
                    for _ in segs:
                        pass
                    print(f"[whisper] préchauffe {tag} en {_time.time() - t0:.2f}s.")
                except Exception as e:
                    print(f"[whisper] préchauffe {tag} KO (ignoré) : {e}")

    # ------------------------------------------------------------------ #
    def _log_input_device(self):
        """Log le device d'entrée par défaut, et utilise VLOCAL_AUDIO_DEVICE si défini."""
        try:
            import sounddevice as sd
            override = os.environ.get("VLOCAL_AUDIO_DEVICE")
            if override:
                # Permet un index numérique ou un nom partiel
                try:
                    override = int(override)
                except ValueError:
                    pass
                sd.default.device = (override, None)  # (input, output)
            dev = sd.query_devices(kind="input")
            self.last_device_name = dev.get("name", "?")
            print(f"[audio]   micro : {self.last_device_name}  "
                  f"(sample_rate par défaut : {dev.get('default_samplerate')})")
        except Exception as e:
            print(f"[audio]   impossible de lister les périphériques : {e}")

    # ------------------------------------------------------------------ #
    @property
    def recording(self) -> bool:
        return self._recording

    def seconds_since_audio(self) -> float:
        """v1.0.11 — secondes depuis le DERNIER échantillon reçu du micro. Détecte un
        flux MORT pendant l'enregistrement (micro débranché, sortie de veille, casque
        BT coupé) : le callback PortAudio cesse d'être appelé -> cet écart grimpe. Une
        SILENCE normale n'augmente PAS l'écart (le callback livre toujours des
        échantillons silencieux). Lecture seule (un float -> atomique pour ce garde-fou)."""
        t = getattr(self, "_last_audio_t", 0.0)
        return max(0.0, _time.monotonic() - t) if t else 0.0

    def list_input_devices(self) -> list:
        """v1.0.21 — Index PortAudio des micros disponibles, micro INTÉGRÉ en tête.
        Sert au repli quand le périphérique par DÉFAUT est coincé ou tenu par une
        autre app (le défaut peut être « valide » et refuser quand même de s'ouvrir).
        Best-effort strict : jamais d'exception, liste vide si indisponible."""
        try:
            import sounddevice as sd
            ranked = []
            # v1.0.22 — query_devices() N'EST PAS BORNÉ et c'est précisément
            # l'appel qui fige quand CoreAudio est coincé (un api_guard
            # PortAudioError sur ce chemin a été journalisé le 16/07). On le
            # borne comme les autres appels audio (patron _close_stream_bounded).
            _devs = _call_bounded(sd.query_devices, 1.0, default=None)
            if _devs is None:
                print("[audio] énumération des périphériques figée (>1s) -> abandonnée.")
                return []
            for i, d in enumerate(_devs):
                try:
                    if int(d.get("max_input_channels", 0)) < 1:
                        continue
                    nm = str(d.get("name", "")).lower()
                    builtin = ("macbook" in nm or "built-in" in nm or "intégr" in nm)
                    ranked.append((0 if builtin else 1, i))
                except Exception:
                    continue
            ranked.sort()
            return [i for _, i in ranked]
        except Exception:
            return []

    def start_recording(self, device=None, prefer=None, deadline=None) -> bool:
        """Ouvre le micro et accumule l'audio en arrière-plan. False si déjà en cours.
        v1.0.21 — `device` (index PortAudio) permet de VISER un micro précis quand le
        défaut est coincé. `device=None` (défaut) = comportement historique INCHANGÉ.
        v1.0.22 — VOIE PRIMAIRE = worker micro (process jetable) : l'app ne touche
        plus PortAudio pour la dictée -> plus JAMAIS corrompue par un device figé.
        `prefer="builtin"` (worker seulement) : résout le micro intégré côté worker
        (l'énumération in-process d'un parent coincé mentait). Les frames arrivent
        dans self._frames comme avant -> chemin de transcription STRICTEMENT
        inchangé. Repli in-process automatique si le worker est indisponible."""
        import sounddevice as sd

        with self._lock:
            if self._recording:
                return False
            self._frames = []
            self._recording = True
            self._rec_via_worker = False
            self._last_audio_t = _time.monotonic()   # v1.0.11 : horloge « flux vivant »

        def _callback(indata, frames, time_info, status):  # noqa: ANN001
            if status:
                pass  # sur-/sous-charge audio : on continue silencieusement
            with self._lock:
                if self._recording:
                    self._frames.append(indata.copy())
                    self._last_audio_t = _time.monotonic()   # v1.0.11 : « flux vivant »

        # ------------------------------------------------------------------
        # v1.0.22 — VOIE PRIMAIRE : worker micro. IMPORTANT : appelé HORS
        # self._lock (contrat anti-deadlock : les premiers chunks peuvent
        # arriver AVANT l'événement "started" et _on_chunk prend le verrou).
        # ------------------------------------------------------------------
        if _MIC_PROC_ENABLED and _micworker is not None:
            rm = self._remote_mic
            if rm is None:
                rm = self._remote_mic = _micworker.RemoteMic(self.sample_rate)

            def _on_chunk(b):
                arr = np.frombuffer(b, dtype=np.float32).copy()
                with self._lock:
                    if self._recording:
                        self._frames.append(arr)
                        self._last_audio_t = _time.monotonic()

            _worker_exc = None
            for _attempt in (1, 2):
                # v1.0.22 — BUDGET DUR (deadline = time.monotonic() cible) :
                # l'utilisateur ne doit JAMAIS attendre plus que le budget que
                # lui accorde l'appelant. Épuisé -> on renonce ICI ; l'appelant
                # rend la main tout de suite et répare EN FOND pour l'appui
                # suivant (l'utilisateur réappuie sous ~3 s d'après le terrain).
                _budget = None
                if deadline is not None:
                    _budget = deadline - _time.monotonic()
                    if _budget <= 0.05:
                        # CRITIQUE : relâcher l'état de capture avant de lever,
                        # sinon _recording resterait True et TOUT start ultérieur
                        # renverrait False (micro bloqué à vie).
                        with self._lock:
                            self._recording = False
                        raise RuntimeError("budget d'ouverture micro épuisé")
                try:
                    rm.start(_on_chunk, device=device, prefer=prefer,
                             budget=_budget)
                    with self._lock:
                        self._rec_via_worker = True
                    self._stream = None
                    return True
                except _micworker.MicStartError as e:
                    # Le worker TOURNE mais le device refuse (occupé/absent) :
                    # échec FRANC remonté à l'escalade app-level (respawn +
                    # micro intégré), comme le levait le chemin historique.
                    with self._lock:
                        self._recording = False
                    raise RuntimeError(str(e))
                except _micworker.WorkerUnavailable as e:
                    # Worker mort ou FIGÉ (déjà tué par RemoteMic) : un respawn
                    # = process neuf = état CoreAudio neuf -> 2e chance quasi
                    # certaine. Après 2 échecs de spawn, repli in-process.
                    _worker_exc = e
                    if _attempt == 1:
                        _rsp = None
                        if deadline is not None:
                            _rsp = max(0.1, deadline - _time.monotonic())
                        if rm.respawn(_rsp):
                            continue
                    break
            # v1.0.22 — le repli in-process (open borné 4 s) ne doit être tenté
            # que si le budget le permet : c'est LE chemin qui se corrompt, il
            # n'a pas à faire exploser l'attente de l'utilisateur.
            if deadline is not None and (deadline - _time.monotonic()) <= 0.05:
                with self._lock:
                    self._recording = False
                raise RuntimeError(f"micro indisponible ({_worker_exc}), budget épuisé")
            print(f"[micworker] indisponible ({_worker_exc}) -> repli in-process.")

        # Si l'ouverture du micro échoue (périphérique absent/occupé), on REMET
        # _recording à False avant de propager : sinon l'état resterait « en
        # cours » et tout futur start_recording renverrait False (micro bloqué).
        try:
            def _mk():
                _kw = dict(
                    samplerate=self.sample_rate,
                    channels=1,
                    dtype="float32",
                    callback=_callback,
                )
                if device is not None:      # v1.0.21 — repli micro explicite
                    _kw["device"] = device
                return sd.InputStream(**_kw)
            # v1.0.9 — ouverture BORNÉE (4 s) : un open figé (device en cours de
            # bascule Bluetooth/casque) ne doit JAMAIS retenir l'appelant ni le
            # verrou du contrôleur à vie -> sinon la dictée devient non
            # redémarrable. En cas de figeage : TimeoutError, on échoue proprement
            # (l'appelant affiche « micro indisponible ») et le prochain essai
            # repart sur un flux neuf.
            self._stream = _open_input_stream_bounded(_mk, timeout=4.0)
            if self._stream is None:
                raise RuntimeError("flux micro non créé")
        except Exception:
            with self._lock:
                self._recording = False
            # Libère un éventuel handle PortAudio (fermeture bornée, sans bloquer).
            _close_stream_bounded(self._stream, 2.0)
            self._stream = None
            raise
        return True

    def rms_recent(self, window_s: float = 0.05) -> float:
        """v11 — Niveau RMS des derniers `window_s` (par défaut 50 ms) du buffer
        en cours d'enregistrement. Sert à animer la wave UI en suivant la voix.
        Coût négligeable : on ne touche que la queue des chunks."""
        nb_samples = int(window_s * self.sample_rate)
        with self._lock:
            if not self._frames:
                return 0.0
            # On parcourt en sens inverse jusqu'à avoir nb_samples au moins
            collected = []
            total = 0
            for chunk in reversed(self._frames):
                collected.append(chunk)
                total += len(chunk)
                if total >= nb_samples:
                    break
        if not collected:
            return 0.0
        audio = np.concatenate(list(reversed(collected)), axis=0).flatten()
        if len(audio) > nb_samples:
            audio = audio[-nb_samples:]
        if audio.size == 0:
            return 0.0
        # RMS normalisé [0..1] approximatif (float32 entre -1 et 1)
        return float(np.sqrt(np.mean(audio.astype(np.float32) ** 2)))

    def recorded_samples(self) -> int:
        """v21 — Nombre d'échantillons capturés depuis le début de la dictée
        en cours (pour le live-tail). 0 si pas d'enregistrement."""
        with self._lock:
            return sum(len(c) for c in self._frames)

    def snapshot_audio(self, start: int, end: int) -> np.ndarray:
        """v21 — COPIE des échantillons [start:end) du buffer en cours
        d'enregistrement (float32 mono), sans arrêter la capture. Sert au
        live-tail : transcrire les fenêtres déjà parlées PENDANT la dictée."""
        with self._lock:
            chunks = list(self._frames)
        if not chunks:
            return np.zeros(0, dtype=np.float32)
        out, pos = [], 0
        for c in chunks:
            c = c.reshape(-1)
            c_end = pos + len(c)
            if c_end > start and pos < end:
                out.append(c[max(0, start - pos):max(0, min(len(c), end - pos))])
            pos = c_end
            if pos >= end:
                break
        if not out:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(out).astype(np.float32, copy=False)

    def prewarm(self, want_small: bool = True) -> None:
        """v21 — Préchargement ANTICIPÉ des modèles, à appeler au DÉBUT de
        l'enregistrement (pendant que l'utilisateur parle) : si l'éco-RAM les
        avait déchargés, le rechargement (~0,7 s ici, jusqu'à plusieurs s sur
        une machine 8 Go au page cache évincé) est MASQUÉ par la parole au lieu
        de s'ajouter à l'attente après la relâche. Mesuré : PAS de pénalité de
        1re inférence post-rechargement dans le même process (4,52 vs 4,58 s)
        -> chargement SEUL, aucune inférence à blanc ici. Sortie inchangée.

        v23 — si le GPU MLX est dispo, on RECHARGE+préchauffe le modèle GPU
        (déchargé à l'inactivité) PENDANT la parole -> la dictée suivante reste
        instantanée. Dans ce cas on évite de charger les modèles CPU (inutiles,
        ils ne servent qu'en repli) pour ne pas gonfler la RAM sur 8 Go."""
        _qos_user_initiated()
        if mlx_engine.available():
            try:
                mlx_engine.warmup()   # recharge le GPU si déchargé (masqué par la parole)
            except Exception as e:
                print(f"[mlx] préchauffe anticipée KO (sans gravité) : {e}")
            return
        try:
            self._ensure_model()
            if want_small:
                self._ensure_fast_model()
            self.last_use = _time.time()
        except Exception as e:
            print(f"[whisper] préchargement anticipé KO (sans gravité) : {e}")

    def stop_recording(self) -> np.ndarray:
        """Ferme le micro et renvoie l'audio capturé (float32 mono 16 kHz)."""
        with self._lock:
            if not self._recording:
                return np.zeros(0, dtype=np.float32)
            self._recording = False
            via_worker = self._rec_via_worker
            self._rec_via_worker = False

        # v1.0.22 — mode worker : le stop se fait DANS le process jetable
        # (borné ; s'il fige, RemoteMic tue le worker — les frames sont déjà
        # ici, rien n'est perdu, et le prochain start respawne). Le process
        # principal ne touche plus PortAudio -> aucun figeage possible ici.
        if via_worker:
            rm = self._remote_mic
            if rm is not None:
                try:
                    rm.stop()
                except Exception:
                    pass
        else:
            # DICT-05 / v1.0.9 : si le périphérique a disparu (casque débranché,
            # device basculé), stop()/close() peut LEVER ou FIGER. On détache la
            # référence AVANT de fermer, et on ferme de façon BORNÉE (hors verrou) :
            # un close figé n'immobilise plus l'appelant -> la dictée reste
            # redémarrable. L'audio déjà capté (self._frames) est concaténé ci-dessous.
            s = self._stream
            self._stream = None
            _close_stream_bounded(s, 3.0)

        with self._lock:
            chunks = self._frames
            self._frames = []

        if not chunks:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(chunks, axis=0).flatten()

    def cancel_recording(self) -> None:
        """v2 — ANNULE l'enregistrement en cours : ferme le micro et JETTE
        l'audio capturé, SANS aucune transcription. Instantané. Sert au bouton
        « Annuler » / touche Échap : l'utilisateur qui veut juste arrêter une
        dictée (même de 5 min) n'attend RIEN. Idempotent et sans erreur si aucun
        enregistrement n'est en cours."""
        with self._lock:
            self._recording = False
            via_worker = self._rec_via_worker
            self._rec_via_worker = False
            s = self._stream
            self._stream = None
        if via_worker:
            rm = self._remote_mic
            if rm is not None:
                try:
                    rm.stop()   # v1.0.22 : borné, kill si figé — jamais bloquant
                except Exception:
                    pass
        else:
            _close_stream_bounded(s, 3.0)   # v1.0.9 : fermeture bornée (anti-figeage device)
        with self._lock:
            self._frames = []

    def reset_audio(self, deep: bool = False) -> bool:
        """v1.0.9 — AUTO-HEAL : remet le sous-système audio de DICTÉE dans un état
        propre quoi qu'il arrive. Détache un flux figé (fermeture bornée), repart
        sans micro ouvert, vide le buffer. Le prochain start_recording ouvre un
        flux NEUF. Idempotent, sans erreur, ne bloque jamais (le supervisor de
        dictée s'en sert pour garantir un redémarrage en quelques secondes).

        v1.0.11 — `deep=True` : en plus du reset léger, ré-init COMPLÈTE de
        PortAudio (récupère un device CoreAudio coincé sans redémarrer l'app).
        Coûteux (re-énumère les périphériques) -> réservé au cas où l'ouverture du
        micro a réellement échoué. `deep=False` (défaut) = comportement v1.0.9
        INCHANGÉ (le superviseur, fréquent, reste léger)."""
        s = None
        try:
            with self._lock:
                self._recording = False
                via_worker = self._rec_via_worker
                self._rec_via_worker = False
                s = self._stream
                self._stream = None
                self._frames = []
        except Exception:
            via_worker = False
            try:
                s = self._stream
                self._stream = None
            except Exception:
                s = None
        _close_stream_bounded(s, 2.5)
        # v1.0.22 — mode worker : le geste profond = RESPAWN du worker (process
        # neuf, état CoreAudio neuf -> récupération GARANTIE et bornée) ; le
        # léger = stop du flux worker. La ré-init in-process ne sert plus que
        # de ceinture si le respawn lui-même échoue (env cassé).
        rm = self._remote_mic
        if rm is not None:
            try:
                if deep:
                    return bool(rm.respawn()) or _reinit_portaudio_bounded(3.0)
                if via_worker:
                    rm.stop()
                return True
            except Exception:
                pass
        if deep:
            # flux déjà détaché ci-dessus -> ré-init PortAudio sûre, bornée, best-effort.
            # v1.0.21 — On REND le verdict : False = ré-init abandonnée (>3 s, PortAudio
            # potentiellement à moitié ré-initialisé). Avant, le résultat était jeté et
            # l'appelant réessayait à l'aveugle -> échec garanti, puis abandon (« micro
            # indisponible » en boucle chez l'utilisateur, jusqu'à 65 fois observées).
            return _reinit_portaudio_bounded(3.0)
        return True

    def hard_mic_reset(self, timeout: float = None) -> bool:
        """v1.0.22 — LE geste de récupération de l'escalade dictée : en mode
        worker, kill -9 + respawn du process de capture (état CoreAudio NEUF,
        borné ~1 s, toujours efficace — c'est ce que « relancer l'app » faisait
        à la main) ; sinon repli sur la ré-init in-process historique."""
        if _MIC_PROC_ENABLED and _micworker is not None:
            rm = self._remote_mic
            if rm is None:
                rm = self._remote_mic = _micworker.RemoteMic(self.sample_rate)
            try:
                if rm.respawn(timeout):
                    return True
            except Exception:
                pass
            # v1.0.22 — sous budget serré, on NE retombe PAS sur la ré-init
            # in-process (3 s de plus, et c'est justement le sous-système
            # corrompu) : le respawn est le seul geste qui répare vraiment.
            if timeout is not None and timeout <= 2.0:
                return False
        return self.reset_audio(deep=True)

    def mic_health_heal(self) -> None:
        """v1.0.22 — SANTÉ PROACTIVE : ping du worker micro AU REPOS ; mort ou
        figé -> respawn PRÉVENTIF. Appelé par le superviseur (~10 s, jamais
        pendant une dictée) : un worker coincé est remplacé PENDANT l'inactivité,
        pas au moment où l'utilisateur appuie -> l'appui part sur un process
        sain (budget « jamais bloqué > 2 s »). Best-effort silencieux."""
        if not (_MIC_PROC_ENABLED and _micworker is not None):
            return
        rm = self._remote_mic
        if rm is None:
            return   # jamais préchauffé -> prewarm_mic_worker s'en charge au boot
        with self._lock:
            if self._recording:
                return
        # v1.0.22 — JAMAIS EN CONCURRENCE AVEC UN APPUI : si le verrou du client
        # worker est déjà pris (une dictée démarre à cet instant), on abandonne
        # le check immédiatement. Sinon le healthcheck préventif pouvait retenir
        # l'ouverture du micro de l'utilisateur (jusqu'à ping+respawn).
        if not rm._lock.acquire(blocking=False):
            return
        try:
            if not rm.healthcheck():
                print("[micworker] worker mort/figé détecté au repos -> respawn préventif.")
                rm.respawn()
        except Exception:
            pass
        finally:
            try:
                rm._lock.release()
            except Exception:
                pass

    def prewarm_mic_worker(self) -> None:
        """v1.0.22 — spawne le worker micro À L'AVANCE (au démarrage de l'app,
        en thread) : la 1re dictée n'attend jamais le spawn (~0,5-1 s en app
        gelée). Micro FERMÉ tant qu'aucune dictée ne démarre (pastille orange
        seulement pendant l'enregistrement). Best-effort silencieux."""
        if not (_MIC_PROC_ENABLED and _micworker is not None):
            return
        try:
            if self._remote_mic is None:
                self._remote_mic = _micworker.RemoteMic(self.sample_rate)
            self._remote_mic.ensure()
            print("[micworker] worker micro préchauffé.")
        except Exception as e:
            print(f"[micworker] préchauffage KO ({e}) -> repli in-process au 1er usage.")

    def _base_kwargs(self, mode: str) -> dict:
        """Construit les kwargs Whisper communs : VAD + langue + glossaire +
        options du mode. Socle partagé via _common_whisper_kwargs().
        v20 — biais lexical via `hotwords` (API dédiée, sans plafond de prompt)
        au lieu d'initial_prompt : cf. build_hotwords()."""
        kwargs = _common_whisper_kwargs(with_thresholds=True)
        kwargs.update(_mode_opts(mode))
        hw = _load_glossary_hotwords()
        if hw:
            kwargs["hotwords"] = hw
        return kwargs

    def transcribe_file(self, file_path: str, on_progress=None,
                        mode: str = "dictee") -> str:
        """Transcrit un FICHIER audio (mp3/mp4/m4a/wav/flac/ogg…). Texte propre.
        Voie « dictée » de l'import : kwargs effectifs inchangés vs l'ancien
        défaut reunion (word_timestamps est de toute façon forcé False ici).
        Anti-hallucination appliqué. Verrou tenu sur toute l'itération du
        générateur paresseux (faster-whisper n'est pas thread-safe)."""
        if not file_path or not os.path.exists(file_path):
            return ""
        kwargs = self._base_kwargs(mode)
        kwargs["word_timestamps"] = False   # texte seul ici
        pieces = []
        model = self._ensure_model()
        with self._infer_lock:
            try:
                segments, info = model.transcribe(file_path, **kwargs)
                total_dur = float(getattr(info, "duration", 0.0) or 0.0)
                for seg in segments:
                    pieces.append(seg.text)
                    if on_progress and total_dur > 0:
                        try:
                            on_progress(min(1.0, float(seg.end) / total_dur))
                        except Exception:
                            pass
            except Exception as e:
                print(f"[engine] transcribe_file échec : {e}")
                return ""
        text = strip_hallucinations(assemble_segments(pieces))
        # Filet quasi mort (texte déjà strippé, cf. docstring du garde) : ne se
        # déclenche que sur un fragment recollé par la passe ponctuation — et
        # jette alors TOUT le texte.
        if _is_known_hallucination(text):
            return ""
        return text

    def transcribe_detailed(self, file_path: str, on_progress=None,
                            mode: str = "reunion") -> dict:
        """v15 — Transcription DÉTAILLÉE (mode RÉUNION) : renvoie le texte ET les
        segments avec horodatage + confidence par mot, pour l'affichage des
        mots peu fiables et le paragraphage par pauses.

        Retour : {
          "text": str,
          "segments": [{"text", "start", "end",
                        "words": [{"word", "prob"}], "avg_conf"}],
          "avg_conf": float,
        }
        """
        empty = {"text": "", "segments": [], "avg_conf": 1.0}
        if not file_path or not os.path.exists(file_path):
            return empty
        kwargs = self._base_kwargs(mode)
        kwargs["word_timestamps"] = True
        seg_list, all_probs = [], []
        model = self._ensure_model()
        with self._infer_lock:
            try:
                segments, info = model.transcribe(file_path, **kwargs)
                total_dur = float(getattr(info, "duration", 0.0) or 0.0)
                for seg in segments:
                    words = []
                    for w in (getattr(seg, "words", None) or []):
                        p = float(getattr(w, "probability", 1.0) or 0.0)
                        # On GARDE les timestamps mot : indispensables pour
                        # attribuer chaque mot au bon locuteur (diarisation fine).
                        words.append({
                            "word": w.word, "prob": round(p, 3),
                            "start": float(getattr(w, "start", seg.start) or seg.start),
                            "end": float(getattr(w, "end", seg.end) or seg.end)})
                        all_probs.append(p)
                    probs = [w["prob"] for w in words]
                    avg = sum(probs) / len(probs) if probs else 1.0
                    seg_list.append({
                        "text": seg.text.strip(),
                        "start": float(seg.start), "end": float(seg.end),
                        "words": words, "avg_conf": round(avg, 3),
                    })
                    if on_progress and total_dur > 0:
                        try:
                            on_progress(min(1.0, float(seg.end) / total_dur))
                        except Exception:
                            pass
            except Exception as e:
                print(f"[engine] transcribe_detailed échec : {e}")
                return empty
        text = strip_hallucinations(
            assemble_segments(s["text"] for s in seg_list))
        avg_conf = round(sum(all_probs) / len(all_probs), 3) if all_probs else 1.0
        return {"text": text, "segments": seg_list, "avg_conf": avg_conf}

    def transcribe_file_batched(self, file_path: str, on_progress=None,
                                mode: str = "reunion", batch_size: int = 2) -> dict:
        """v20 — Transcription BATCHED d'un FICHIER importé (BatchedInference-
        Pipeline) : segmentation VAD puis décodage de `batch_size` segments EN
        PARALLÈLE sur le même modèle int8. RÉSERVÉ À L'IMPORT (audio entièrement
        disponible) — jamais au live ni à la dictée micro.

        MESURÉ (corpus FR, M-series 4 threads) : -25 % de temps mur ET -26 % de
        cœurs-secondes (moins de chauffe) vs l'incrémental pace=0, qualité
        équivalente (WER 0,0307 vs 0,0391 incrémental / 0,0279 monobloc sur
        réunion 3,8 min). batch_size=2 : pic RSS = celui de l'incrémental
        (~2,8 Go process) ; batch_size=4 : +1,4 Go -> réservé aux machines
        larges. L'appelant choisit batch_size selon la RAM disponible.

        Même format de retour que transcribe_detailed. Le fichier est décodé en
        STREAMING par faster-whisper (chemin passé tel quel : aucun chargement
        intégral côté app)."""
        empty = {"text": "", "segments": [], "avg_conf": 1.0}
        if not file_path or not os.path.exists(file_path):
            return empty
        kwargs = self._base_kwargs(mode)
        kwargs["word_timestamps"] = True
        seg_list, all_probs = [], []
        model = self._ensure_model()
        with self._infer_lock:
            try:
                from faster_whisper import BatchedInferencePipeline
                if self._batched is None or getattr(self._batched, "model", None) is not model:
                    self._batched = BatchedInferencePipeline(model=model)
                segments, info = self._batched.transcribe(
                    file_path, batch_size=int(batch_size), **kwargs)
                total_dur = float(getattr(info, "duration", 0.0) or 0.0)
                for seg in segments:
                    words = []
                    for w in (getattr(seg, "words", None) or []):
                        p = float(getattr(w, "probability", 1.0) or 0.0)
                        words.append({
                            "word": w.word, "prob": round(p, 3),
                            "start": float(getattr(w, "start", seg.start) or seg.start),
                            "end": float(getattr(w, "end", seg.end) or seg.end)})
                        all_probs.append(p)
                    probs = [w["prob"] for w in words]
                    avg = sum(probs) / len(probs) if probs else 1.0
                    seg_list.append({
                        "text": seg.text.strip(),
                        "start": float(seg.start), "end": float(seg.end),
                        "words": words, "avg_conf": round(avg, 3),
                    })
                    if on_progress and total_dur > 0:
                        try:
                            on_progress(min(1.0, float(seg.end) / total_dur))
                        except Exception:
                            pass
            except Exception as e:
                print(f"[engine] transcribe_file_batched échec : {e}")
                return empty
        text = strip_hallucinations(
            assemble_segments(s["text"] for s in seg_list))
        avg_conf = round(sum(all_probs) / len(all_probs), 3) if all_probs else 1.0
        return {"text": text, "segments": seg_list, "avg_conf": avg_conf}

    def transcribe_import(self, wav_path: str, batch_size: int = 2,
                          pace: float = 0.5, on_progress=None,
                          on_window=None, piece_s: float = 120.0,
                          mode: str = "reunion") -> dict:
        """v22 — IMPORT de fichier SILENCIEUX : la vitesse du batched, le
        profil thermique de l'incrémental. Tout se passe EN ARRIÈRE-PLAN,
        interface inchangée (seule la barre de progression existante avance).

        Le batched plein-fichier (v20) décodait un import de 15 min en charge
        CPU SOUTENUE de plusieurs minutes -> ventilateurs. Ici : le WAV est
        découpé en MORCEAUX (~90 s) coupés aux VRAIS silences, chaque morceau
        est décodé en batched (rapide), puis le CPU RESPIRE entre les morceaux
        (pause = pace x temps CPU du morceau -> duty cycle ~60 %).

        Qualité : coupes aux silences = validé texte-identique (corpus) ;
        batched intra-morceau = qualité équivalente mesurée (WER 0,031 vs
        0,034 monobloc). RAM : np.memmap, jamais plus d'un morceau en float32.
        on_window(audio, sr, offset) alimente le diariseur EN LIGNE.
        Format de retour = transcribe_detailed."""
        import time as _t
        empty = {"text": "", "segments": [], "avg_conf": 1.0}
        if not wav_path or not os.path.exists(wav_path):
            return empty
        sr = SAMPLE_RATE
        audio = np.memmap(wav_path, dtype="<i2", mode="r", offset=44)
        n = len(audio)
        if n == 0:
            return empty
        piece_t = int(piece_s * sr)          # cible de morceau (rafale CPU brève)
        search = int(12.0 * sr)              # fenêtre de recherche de silence
        step = max(1, int(0.1 * sr))
        kwargs = self._base_kwargs(mode)
        kwargs["word_timestamps"] = True
        seg_all, all_probs = [], []
        pos = 0
        model = self._ensure_model()
        from faster_whisper import BatchedInferencePipeline
        while pos < n:
            end = min(pos + piece_t + search, n)
            cut = end
            if end - pos > piece_t:          # cherche un silence après la cible
                for c in range(pos + piece_t, end - step, step):
                    w = audio[c:c + step].astype(np.float32) / 32768.0
                    if float(np.sqrt(np.mean(w ** 2))) < SILENCE_RMS:
                        cut = c + step // 2
                        break
            piece = audio[pos:cut].astype(np.float32) / 32768.0
            t0 = pos / sr
            ct = _t.perf_counter()
            try:
                with self._infer_lock:
                    if self._batched is None or getattr(self._batched, "model", None) is not model:
                        self._batched = BatchedInferencePipeline(model=model)
                    segments, _info = self._batched.transcribe(
                        piece, batch_size=int(batch_size), **kwargs)
                    piece_segs = []
                    for seg in segments:
                        words = []
                        for w in (getattr(seg, "words", None) or []):
                            p = float(getattr(w, "probability", 1.0) or 0.0)
                            words.append({
                                "word": w.word, "prob": round(p, 3),
                                "start": float(getattr(w, "start", seg.start) or seg.start) + t0,
                                "end": float(getattr(w, "end", seg.end) or seg.end) + t0})
                            all_probs.append(p)
                        probs = [w["prob"] for w in words]
                        piece_segs.append({
                            "text": seg.text.strip(),
                            "start": float(seg.start) + t0,
                            "end": float(seg.end) + t0,
                            "words": words,
                            "avg_conf": round(sum(probs) / len(probs), 3) if probs else 1.0})
                seg_all.extend(piece_segs)
            except Exception as e:
                print(f"[import] morceau batched KO ({e}) — repli fenêtré.")
                res = self.transcribe_window(piece, time_offset=t0, mode=mode)
                piece_segs = res.get("segments", [])
                seg_all.extend(piece_segs)
                for s in piece_segs:
                    all_probs.extend(w.get("prob", 1.0) for w in s.get("words", []))
            cpu = _t.perf_counter() - ct
            if on_window is not None:
                try:
                    on_window(piece, sr, t0)
                except Exception:
                    pass
            if on_progress is not None:
                try:
                    on_progress(min(0.99, cut / n))
                except Exception:
                    pass
            pos = cut
            # RESPIRATION anti-chauffe entre morceaux (jamais après le dernier)
            if pace > 0 and pos < n:
                _t.sleep(min(10.0, cpu * pace))
        text = strip_hallucinations(
            assemble_segments(s["text"] for s in seg_all))
        avg_conf = round(sum(all_probs) / len(all_probs), 3) if all_probs else 1.0
        if on_progress is not None:
            try:
                on_progress(1.0)
            except Exception:
                pass
        return {"text": text, "segments": seg_all, "avg_conf": avg_conf}

    def transcribe_import_mlx(self, audio_np, sr: int = SAMPLE_RATE,
                              on_progress=None, chunk_s: float = 300.0) -> dict:
        """v27 — IMPORT PLEIN DÉBIT sur GPU MLX : passe l'audio par GROS BLOCS
        (~10 min) à mlx_whisper, qui gère nativement son fenêtrage 30 s -> on
        évite l'overhead des fenêtres 25 s de transcribe_incremental (×28 temps
        réel mesuré, mots inclus, vs ~×13). Blocs coupés au CREUX D'ÉNERGIE
        (jamais en plein mot), conversion float32 PAR BLOC (RAM bornée ~40 Mo,
        memmap int16 accepté), horodatages décalés, progression par bloc.
        RÉSERVÉ À L'IMPORT RÉUNION (jamais la dictée). Lève si MLX indisponible
        (l'appelant retombe sur transcribe_incremental).

        RAM (mesuré, 34 min) : blocs 5 min -> pic ~1,5 Go (vs 2,0 Go à 10 min).
        Dérive du pool Metal ~18 Mo/bloc : un reset périodique a été testé et
        REJETÉ (le rechargement crée un pic transitoire +450 Mo, pire que la
        dérive). Au-delà de ~45 min, l'appelant route vers transcribe_
        incremental (RAM stable éprouvée) : ce chemin est réservé aux imports
        <= ~45 min, enveloppe verrouillée par tests/rd/reunion_gate.py."""
        if not mlx_engine.available():
            raise RuntimeError("MLX indisponible")
        empty = {"text": "", "segments": [], "avg_conf": 1.0}
        if audio_np is None or len(audio_np) == 0:
            return empty

        def _f32(chunk):
            if chunk.dtype == np.int16:
                return chunk.astype(np.float32) / 32768.0
            return np.asarray(chunk, dtype=np.float32)

        n = len(audio_np)
        cs = int(chunk_s * sr)
        seg_list = []
        pos = 0
        n_chunk = 0
        while pos < n:
            end = min(n, pos + cs)
            if end < n:
                # coupe au creux d'énergie des 2 dernières secondes du bloc
                # (RMS glissant 50 ms) -> pas de mot tronqué à la jointure.
                tail = _f32(np.asarray(audio_np[end - int(2.0 * sr):end]))
                w = max(1, int(0.05 * sr))
                k = np.ones(w, dtype=np.float32) / w
                e = np.convolve(tail * tail, k, mode="valid")
                end = end - int(2.0 * sr) + int(e.argmin()) + w // 2
            d = self._mlx_detailed(_f32(np.asarray(audio_np[pos:end])),
                                   pos / float(sr), "reunion")
            seg_list.extend(d.get("segments", []))
            pos = end
            n_chunk += 1
            if on_progress:
                try:
                    on_progress(min(1.0, pos / float(n)))
                except Exception:
                    pass
        text = strip_hallucinations(
            assemble_segments(s["text"] for s in seg_list))
        probs = [w["prob"] for s in seg_list for w in (s.get("words") or [])]
        avg = round(sum(probs) / len(probs), 3) if probs else 1.0
        return {"text": text, "segments": seg_list, "avg_conf": avg}

    def transcribe_incremental(self, audio_np, sr: int = SAMPLE_RATE,
                               on_progress=None, on_window=None, pace: float = 0.6):
        """Transcrit un LONG audio (import) EN FENÊTRES ~25 s coupées aux silences,
        avec PACING entre fenêtres -> la charge CPU est LISSÉE (pas de saturation
        soutenue = pas de chauffe/ventilateur), et le traitement se fait « au fur
        et à mesure » (progression). Le VAD interne de transcribe_window saute les
        silences (moins de travail). on_window(pcm, sr, offset_s) est appelé par
        fenêtre (ex. alimenter le diariseur WhoTalks en ligne). Renvoie le même
        format que transcribe_detailed.

        pace : durée de pause = pace × (temps CPU de la fenêtre). 0.6 -> ~62% de
        cycle actif (refroidit entre les fenêtres) ; 0 -> pas de pause (rapide).

        v20 (RAM) — accepte aussi un tableau INT16 (typiquement un np.memmap du
        WAV sur disque) : la conversion float32 se fait PAR FENÊTRE (~1,3 Mo) au
        lieu de matérialiser tout le fichier en float32 (~460 Mo pour 2 h).
        int16/32768 par fenêtre == conversion globale puis découpe -> sortie
        byte-identique."""
        import time as _t

        def _f32(chunk):
            if chunk.dtype == np.int16:
                return chunk.astype(np.float32) / 32768.0
            return chunk

        empty = {"text": "", "segments": [], "avg_conf": 1.0}
        if audio_np is None or len(audio_np) == 0:
            return empty
        win_t = int(WINDOW_TARGET_S * sr)
        win_max = int(WINDOW_MAX_S * sr)
        sil = int(SILENCE_SEARCH_S * sr)  # fenêtre de recherche du silence
        step = max(1, int(0.1 * sr))
        n = len(audio_np)
        pos = 0
        seg_all, all_probs = [], []
        while pos < n:
            end = min(pos + win_max, n)
            cut = end
            # cherche un point de SILENCE après la cible (ne pas couper un mot)
            if end - pos > win_t:
                for c in range(pos + win_t, min(pos + win_t + sil, end - step), step):
                    w = _f32(audio_np[c:c + step])
                    if float(np.sqrt(np.mean(w ** 2))) < SILENCE_RMS:
                        cut = c
                        break
            window = _f32(audio_np[pos:cut])
            t0 = pos / sr
            ct = _t.perf_counter()
            try:
                res = self.transcribe_window(window, time_offset=t0, mode="reunion")
            except Exception as e:
                print(f"[engine] fenêtre import KO ({e}) — ignorée.")
                res = {"segments": []}
            cpu = _t.perf_counter() - ct
            for s in res.get("segments", []):
                seg_all.append(s)
                all_probs.extend(w.get("prob", 1.0) for w in s.get("words", []))
            if on_window is not None:
                try:
                    on_window(window, sr, t0)
                except Exception:
                    pass
            if on_progress is not None:
                try:
                    on_progress(min(0.99, cut / n))
                except Exception:
                    pass
            pos = cut
            # PACING : on laisse le CPU respirer entre les fenêtres (anti-chauffe).
            if pace > 0 and pos < n:
                _t.sleep(min(6.0, cpu * pace))
        text = strip_hallucinations(
            assemble_segments(s["text"] for s in seg_all))
        avg_conf = round(sum(all_probs) / len(all_probs), 3) if all_probs else 1.0
        if on_progress is not None:
            try:
                on_progress(1.0)
            except Exception:
                pass
        return {"text": text, "segments": seg_all, "avg_conf": avg_conf}

    def transcribe_window(self, audio_np: np.ndarray, time_offset: float = 0.0,
                          mode: str = "reunion") -> dict:
        """v2 — Transcrit une FENÊTRE audio (float32) avec segments + confidence,
        horodatages décalés de `time_offset`. Transcription incrémentale d'une
        réunion PENDANT l'enregistrement.

        Toujours TURBO (mesuré ~7x temps réel) : suit largement le direct, même
        2 h, et garde la haute fidélité (noms propres, acronymes, jargon
        technique). Le small était 2x plus rapide mais massacrait le jargon ->
        FIDÉLITÉ privilégiée pour les réunions (décision produit mesurée).
        Le glossaire (biais hotwords + passe 2) renforce encore."""
        empty = {"text": "", "segments": [], "avg_conf": 1.0}
        if audio_np is None or len(audio_np) == 0:
            return empty
        rms, peak = _audio_stats(audio_np)
        if _is_silent(rms, peak):
            return empty
        # v23 — GPU MLX si dispo (réunion live + import incrémental, ~7x). La
        # fenêtre passe au GPU avec word_timestamps ; repli CPU si le GPU lève.
        if mlx_engine.available():
            try:
                return self._mlx_detailed(audio_np, time_offset, mode)
            except Exception as e:
                print(f"[mlx] window KO ({e}) -> repli CPU.")
        model = self._ensure_model()
        kwargs = self._base_kwargs(mode)
        kwargs["word_timestamps"] = True
        seg_list, all_probs = [], []
        with self._infer_lock:
            try:
                segments, _info = model.transcribe(audio_np, **kwargs)
                for seg in segments:
                    words = []
                    for w in (getattr(seg, "words", None) or []):
                        p = float(getattr(w, "probability", 1.0) or 0.0)
                        words.append({
                            "word": w.word, "prob": round(p, 3),
                            "start": float(getattr(w, "start", seg.start) or seg.start) + time_offset,
                            "end": float(getattr(w, "end", seg.end) or seg.end) + time_offset})
                        all_probs.append(p)
                    probs = [w["prob"] for w in words]
                    avg = sum(probs) / len(probs) if probs else 1.0
                    seg_list.append({
                        "text": seg.text.strip(),
                        "start": float(seg.start) + time_offset,
                        "end": float(seg.end) + time_offset,
                        "words": words, "avg_conf": round(avg, 3),
                    })
            except Exception as e:
                print(f"[engine] transcribe_window échec : {e}")
                return empty
        text = strip_hallucinations(assemble_segments(s["text"] for s in seg_list))
        avg_conf = round(sum(all_probs) / len(all_probs), 3) if all_probs else 1.0
        return {"text": text, "segments": seg_list, "avg_conf": avg_conf}

    def _mlx_text(self, audio_np, mode: str = "dictee") -> str:
        """v23 — Transcription GPU (MLX/Metal) -> texte FR propre, AVEC le même
        post-traitement que le chemin CPU : biais glossaire (en initial_prompt,
        l'API hotwords étant propre à faster-whisper) + anti-hallucination.
        Le modèle est le MÊME turbo (parité qualité prouvée), 7x plus rapide.
        Lève en cas d'échec GPU -> l'appelant retombe sur le CPU."""
        hw = _load_glossary_hotwords()
        prompt = ("Transcription en français. Termes : " + hw + ".") if hw else None
        # v1.1.0 — timeout DICTÉE proportionnel à l'audio (dictation_gpu_timeout),
        # sous le budget de finalisation (app._finalize_budget_s, même formule + 3 s).
        # Le plafond fixe de 22 s (v1.0.6) supposait un reliquat court grâce au
        # live-tail ; quand celui-ci ne trouvait pas de silence, un reliquat de
        # 6 min dépassait 22 s et condamnait le GPU pour toute la session.
        # v1.0.22 — PRIORITÉ GPU : ce chemin est celui de la DICTÉE (l'utilisateur
        # attend, curseur clignotant). Il passe DEVANT un préchauffe, un bloc
        # d'import (300 s d'audio) ou une fenêtre de réunion déjà en file — sinon
        # il consommait son propre timeout de 22 s à faire la queue, et déclarait
        # un faux « gel Metal » qui condamnait le GPU pour toute la session.
        _budget = dictation_gpu_timeout(len(audio_np) / float(SAMPLE_RATE))
        r = mlx_engine.transcribe(audio_np, language="fr", initial_prompt=prompt,
                                  temperature=(0.0, 0.2, 0.4, 0.6), _timeout=_budget,
                                  _prio=getattr(mlx_engine, "PRIO_DICTATION", 0))
        # v3.3.1 — aligne la dictée sur le chemin réunion : collapse_repetition_loops
        # supprime les queues parasites (« 3 3 3 », « 4. ») sur les fins en silence.
        # Post-traitement texte uniquement : zéro impact vitesse / RAM, qualité ↑.
        text = collapse_repetition_loops(
            strip_hallucinations(assemble_segments([r.get("text", "")])))
        if _is_known_hallucination(text):
            return ""
        self.last_model = "mlx-turbo"
        return text

    def _mlx_detailed(self, audio_np, time_offset: float = 0.0,
                      mode: str = "reunion") -> dict:
        """v23 — MLX -> format DÉTAILLÉ (segments + word_timestamps + confiance),
        identique à transcribe_window/transcribe_detailed CPU, pour la réunion et
        l'import sur GPU. Horodatages décalés de time_offset. Lève si GPU KO."""
        hw = _load_glossary_hotwords()
        prompt = ("Transcription en français. Termes : " + hw + ".") if hw else None
        # v28 — anti-hallucination RÉUNION : saute les passages de silence que
        # Whisper « remplit » (ex. « Sous-titrage Société Radio-Canada »). Sans
        # effet sur la dictée (qui n'appelle pas ce chemin).
        r = mlx_engine.transcribe(audio_np, language="fr", initial_prompt=prompt,
                                  word_timestamps=True,
                                  hallucination_silence_threshold=2.0)
        seg_list, all_probs = [], []
        for seg in r.get("segments", []):
            words = []
            for w in (seg.get("words") or []):
                p = float(w.get("probability", 1.0) or 0.0)
                ws = float(w.get("start", seg.get("start", 0.0)) or 0.0) + time_offset
                we = float(w.get("end", seg.get("end", 0.0)) or 0.0) + time_offset
                words.append({"word": w.get("word", ""), "prob": round(p, 3),
                              "start": ws, "end": we})
                all_probs.append(p)
            probs = [x["prob"] for x in words]
            seg_list.append({
                "text": collapse_repetition_loops((seg.get("text") or "").strip()),
                "start": float(seg.get("start", 0.0)) + time_offset,
                "end": float(seg.get("end", 0.0)) + time_offset,
                "words": words,
                "avg_conf": round(sum(probs) / len(probs), 3) if probs else 1.0})
        text = collapse_repetition_loops(
            strip_hallucinations(assemble_segments(s["text"] for s in seg_list)))
        avg = round(sum(all_probs) / len(all_probs), 3) if all_probs else 1.0
        return {"text": text, "segments": seg_list, "avg_conf": avg}

    def transcribe(self, audio_np: np.ndarray, vad: bool = True,
                   mode: str = "dictee", fast: bool = False) -> str:
        """Transcrit un float32 mono 16 kHz en texte FR propre.
        Mode 'dictee' = greedy rapide (latence minimale).
        fast=True : utilise le modèle small (dictée rapide) si disponible.
        v23 — Si le GPU MLX est dispo, il prime (turbo partout, ~7x, qualité
        turbo) ; repli CPU automatique et silencieux sinon."""
        if audio_np is None or len(audio_np) == 0:
            return ""
        rms, peak = _audio_stats(audio_np)
        if _is_silent(rms, peak):
            return ""  # silence -> pas de transcription (anti-Amara)
        if mlx_engine.available():
            try:
                return self._mlx_text(audio_np, mode)
            except Exception as e:
                print(f"[mlx] transcribe KO ({e}) -> repli CPU.")
        model = self._ensure_model()
        if fast:
            # Référence LOCALE au small : un unload_fast_model() concurrent ne
            # peut plus nullifier la référence entre le check et l'inférence.
            fm = self._ensure_fast_model()
            if fm:
                model = fm
        kwargs = self._base_kwargs(mode)
        # NB : aucun appelant ne passe vad=False aujourd'hui (vad_filter=True
        # est déjà posé par _base_kwargs) ; paramètre conservé (API publique).
        kwargs["vad_filter"] = vad
        # v1.0.23 — ACQUISITION BORNÉE SUR LE CHEMIN DICTÉE. `_infer_lock` est
        # PARTAGÉ avec la transcription de fichier et de réunion, qui le tiennent
        # pendant TOUT le fichier (plusieurs minutes sur un import d'une heure).
        # Une dictée tombée dessus attendait sans borne. On lève au bout de 20 s :
        # `_do_finalize` attrape déjà TimeoutError, informe l'utilisateur et rend
        # la main — au lieu de le laisser croire que la dictée est perdue.
        if not self._infer_lock.acquire(timeout=20.0):
            print("[engine] moteur occupé (import/réunion) > 20 s -> dictée rendue.")
            raise TimeoutError("moteur occupé par une autre transcription")
        try:
            try:
                segments, _info = model.transcribe(audio_np, **kwargs)
                text = assemble_segments(seg.text for seg in segments)
            except Exception as e:
                print(f"[engine] transcribe échec : {e}")
                return ""
        finally:
            self._infer_lock.release()
        text = strip_hallucinations(text)
        # Filet quasi mort (texte déjà strippé, cf. docstring du garde) : ne se
        # déclenche que sur un fragment recollé par la passe ponctuation — et
        # jette alors TOUT le texte.
        if _is_known_hallucination(text):
            return ""
        return text

    # v2 — Seuils de routage cascade « auto intelligent », CALIBRÉS sur la
    # distribution réelle de confiance de small (mesurée) :
    #   audio propre  -> avg 0,945-0,971, 0 % de mots <0,3
    #   audio difficile-> avg 0,910, 3,6 % de mots <0,3
    # On garde small (rapide, ~1,3 s) UNIQUEMENT si l'audio est clairement net.
    # Discriminateur principal = fraction de mots TRÈS incertains (<0,3), PAS un
    # seul mot (l'erreur de l'ancienne cascade qui escaladait quasi toujours).
    SMALL_AVG_OK = 0.90          # confiance moyenne haute
    SMALL_FRAC03_MAX = 0.02      # < 2 % de mots très incertains (<0,3)
    SMALL_FRAC05_MAX = 0.10      # < 10 % de mots incertains (<0,5)
    # R&D vitesse v19 : au-delà de ce budget, la passe small est quasi toujours
    # suivie d'une escalade turbo (re-transcription complète) -> elle est JETÉE
    # (25-32 % du temps gaspillé). On va alors directement en turbo : sortie =
    # exactement ce que la cascade aurait gardé (zéro perte qualité), -25/-32 %.
    # Seuil conservateur : les courtes dictées restent sur la cascade (small y
    # suffit souvent et c'est plus rapide).
    ADAPTIVE_DIRECT_TURBO_S = 28.0

    def _small_with_conf(self, audio_np):
        """Transcrit avec small SANS biais glossaire (confiance stable et
        comparable d'un appel à l'autre). Renvoie
        (texte, avg_prob, frac_low03, frac_low05)."""
        # Socle commun SANS les seuils explicites (with_thresholds=False : la
        # passe small décode avec les DÉFAUTS de la lib, inchangé) et SANS
        # hotwords (intentionnel, cf. docstring). temperature=0.0 SANS
        # filet : VOULU — pas de re-décodage à température croissante ici,
        # pour des confiances stables et comparables d'un appel à l'autre.
        kwargs = _common_whisper_kwargs(with_thresholds=False)
        kwargs["beam_size"] = 1
        kwargs["best_of"] = 1
        kwargs["temperature"] = 0.0
        kwargs["word_timestamps"] = True
        with self._infer_lock:
            segments, _info = self._fast_model.transcribe(audio_np, **kwargs)
            probs, pieces = [], []
            for seg in segments:
                pieces.append(seg.text)
                for w in (getattr(seg, "words", None) or []):
                    probs.append(float(getattr(w, "probability", 1.0) or 0.0))
            text = assemble_segments(pieces)
        if not probs:
            return strip_hallucinations(text), 1.0, 0.0, 0.0
        avg = sum(probs) / len(probs)
        frac03 = sum(1 for p in probs if p < 0.3) / len(probs)
        frac05 = sum(1 for p in probs if p < 0.5) / len(probs)
        return strip_hallucinations(text), avg, frac03, frac05

    def transcribe_adaptive(self, audio_np: np.ndarray):
        """v2 — CASCADE « AUTO INTELLIGENT » (small -> turbo) tunée.

        small transcrit + estime sa confiance. Si l'audio est CLAIREMENT net
        (seuils calibrés) -> on GARDE small (~1,3 s, 2,6x plus rapide que turbo,
        fidélité quasi identique sur le français courant). Au moindre doute
        global -> turbo (fidélité max). Le glossaire (côté app) corrige les
        termes connus quel que soit le modèle retenu.

        Renvoie (texte, modele). Repli turbo si small indisponible."""
        if audio_np is None or len(audio_np) == 0:
            return "", "none"
        rms, peak = _audio_stats(audio_np)
        if _is_silent(rms, peak):
            return "", "none"
        # v23 — GPU MLX dispo : la cascade n'a plus de raison d'être (turbo y
        # est plus RAPIDE que small ET de qualité turbo). On fait turbo partout.
        if mlx_engine.available():
            try:
                return self._mlx_text(audio_np, "dictee"), "mlx"
            except Exception as e:
                print(f"[mlx] adaptive KO ({e}) -> cascade CPU.")
        # Audio long -> turbo DIRECT (on saute la passe small qui serait jetée à
        # l'escalade). Sortie identique à l'escalade, sans le double-travail.
        dur_s = len(audio_np) / float(SAMPLE_RATE)
        if dur_s > self.ADAPTIVE_DIRECT_TURBO_S:
            print(f"[cascade] audio {dur_s:.0f}s > {self.ADAPTIVE_DIRECT_TURBO_S:.0f}s "
                  "-> turbo direct (pas de small jeté).")
            return self.transcribe(audio_np, mode="dictee"), "turbo"
        if not self._ensure_fast_model():
            return self.transcribe(audio_np, mode="dictee"), "turbo"
        try:
            text_s, avg, frac03, frac05 = self._small_with_conf(audio_np)
        except Exception as e:
            print(f"[cascade] small KO ({e}) — turbo direct.")
            return self.transcribe(audio_np, mode="dictee"), "turbo"
        # NB : _is_known_hallucination est un filet quasi mort post-strip
        # (cf. sa docstring) — text_s sort déjà strippé de _small_with_conf.
        if text_s and not _is_known_hallucination(text_s) \
                and avg >= self.SMALL_AVG_OK \
                and frac03 <= self.SMALL_FRAC03_MAX \
                and frac05 <= self.SMALL_FRAC05_MAX:
            print(f"[cascade] small suffit (avg={avg:.2f}, "
                  f"frac<.3={frac03:.2f}, frac<.5={frac05:.2f}).")
            return text_s, "small"
        print(f"[cascade] escalade turbo (avg={avg:.2f}, "
              f"frac<.3={frac03:.2f}, frac<.5={frac05:.2f}).")
        text_t = self.transcribe(audio_np, mode="dictee")
        if text_t:
            return text_t, "turbo"
        # turbo a renvoyé '' (échec avalé) : le texte rendu vient de small ->
        # on renvoie la VRAIE branche (last_model/overlay justes).
        return text_s, "small"

    def stop_and_transcribe(self, fast: bool = False,
                            adaptive: bool = False, live_tail=None,
                            audio=None) -> str:
        """Arrête la capture et transcrit dans la foulée (FINAL). '' si rien/silence.

          adaptive=True (défaut dictée « auto ») : cascade intelligente —
              small si l'audio est net (~1,3 s), turbo au moindre doute global.
          fast=True  : small forcé (mode dictée « rapide »).
          sinon      : turbo direct (mode dictée « fidèle »).

        v21 — live_tail=(textes, tail_start) : des fenêtres coupées aux VRAIS
        silences ont déjà été transcrites PENDANT l'enregistrement (dictée
        longue) ; on ne transcrit que le RELIQUAT et on assemble. VALIDÉ sur
        corpus : textes normalisés STRICTEMENT IDENTIQUES au single-pass
        (coupe aux silences + condition_on_previous_text=False rendent le
        décodage indépendant par fenêtre), latence 13,0->4,6 s et 18,6->4,2 s.

        v1.0.22 — `audio` : PCM déjà extrait par l'appelant (chaînage : la
        capture est fermée dès le relâchement, la transcription attend son tour
        FIFO). `audio=None` = comportement historique (stop + transcribe)."""
        if audio is None:
            audio = self.stop_recording()
        rms, peak = _audio_stats(audio)
        self.last_rms = rms
        self.last_peak = peak
        if _is_silent(rms, peak):
            print(f"[audio]   aucun son détecté (rms={rms:.4f}, peak={peak:.4f}). "
                  f"Device : {self.last_device_name}. "
                  "Vérifie permission micro + bon device d'entrée.")
            return ""
        if live_tail is not None and not fast:
            texts, tail_start = live_tail
            if texts and 0 < tail_start <= len(audio):
                tail_text = self.transcribe(audio[tail_start:])
                self.last_model = "turbo"
                full = strip_hallucinations(
                    assemble_segments(list(texts) + [tail_text]))
                print(f"[live-tail] {len(texts)} fenêtre(s) pré-transcrites + "
                      f"reliquat {len(audio[tail_start:]) / SAMPLE_RATE:.0f}s.")
                return full
        if adaptive and not fast:
            text, _model = self.transcribe_adaptive(audio)
            self.last_model = _model            # v19 : branche cascade (affichage overlay)
            return text
        self.last_model = "small" if fast else "turbo"
        return self.transcribe(audio, vad=True, fast=fast)
