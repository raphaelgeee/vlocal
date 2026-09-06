#!/usr/bin/env python3
"""Vlocal — journal d'événements/erreurs STRUCTURÉ (centralisation « tour de contrôle »).

POURQUOI (v3.2.9) : les logs texte (vlocal.log) sont tronqués à chaque démarrage et
non catégorisés -> impossible de dénombrer les incidents et de centraliser les bugs.
Ce module fournit un journal :
  - APPEND-ONLY, horodaté ISO, une ligne JSON par événement (jsonl) ;
  - CATÉGORISÉ par CODE stable (E.*) -> on peut compter « combien de gpu_timeout » ;
  - PERSISTANT avec rotation (jamais perdu au reboot, borné à ~2 Mo + 1 backup) ;
  - SANS DONNÉE SENSIBLE : on ne journalise JAMAIS le texte transcrit, seulement des
    métadonnées (durées, codes, booléens) -> respecte la promesse 100% local/privé.

Best-effort : aucune fonction ne lève jamais (un échec de log ne doit jamais casser
la dictée). Lu par l'admin (tour de contrôle) via recent()/summary(), remontée opt-in.
"""
import json
import os
import threading
import time

_DIR = os.path.expanduser("~/Library/Application Support/Vlocal")
_LOG = os.path.join(_DIR, "events.jsonl")
_MAX_BYTES = 2_000_000          # rotation à ~2 Mo (garde 1 backup .1)
_lock = threading.Lock()


class E:
    """Codes d'événements STABLES (ne pas renommer : sert au comptage côté admin)."""
    GPU_TIMEOUT = "gpu_timeout"            # transcription GPU calée -> watchdog 60s
    GPU_FALLBACK_CPU = "gpu_fallback_cpu"  # bascule GPU->CPU (auto-correction)
    DIAR_FALLBACK = "diar_fallback"        # v1.0.25 — diarisation tombée sur le
                                           # repli tuiles (worker d'empreintes KO) :
                                           # résultat nettement moins bon, et la
                                           # bascule était invisible jusqu'ici
    FINALIZE_TIMEOUT = "finalize_timeout"  # finalisation figée -> watchdog 30s
    MIC_DENIED = "mic_denied"              # permission micro refusée
    MIC_DEVICE_FAIL = "mic_device_fail"    # périphérique audio indisponible/déconnecté
    MIC_RECOVERED = "mic_recovered"        # micro récupéré tout seul (repli périphérique explicite)
    SILENCE = "silence"                    # son capté trop faible / vide
    TRANSCRIBE_FAIL = "transcribe_fail"    # exception pendant la transcription
    INSERT_FAIL = "insert_fail"            # insertion clavier KO (repli presse-papier)
    CLIPBOARD_FAIL = "clipboard_fail"      # copie presse-papier KO
    MODEL_MISSING = "model_missing"        # modèle absent/corrompu
    DISK_FULL = "disk_full"                # espace disque insuffisant
    MEETING_FAIL = "meeting_fail"          # pipeline réunion en échec
    UPDATE_FAIL = "update_fail"            # mise à jour KO
    API_GUARD = "api_guard"                # exception attrapée par @_api_safe
    ENGINE_LOAD_FAIL = "engine_load_fail"  # moteur Whisper n'a pas pu se charger (modèle corrompu / OOM)
    MAIN_FREEZE_KILL = "main_freeze_kill"  # thread principal figé > seuil -> arrêt forcé (deadman switch)


def log(code, level="error", **context):
    """Journalise un événement : (code stable, niveau, contexte métadonnées).
    Best-effort, ne lève jamais. `text` est explicitement EXCLU du contexte."""
    try:
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "code": str(code),
               "level": str(level)}
        if context:
            rec["ctx"] = {k: v for k, v in context.items()
                          if k != "text" and v is not None}
        line = json.dumps(rec, ensure_ascii=False)
        with _lock:
            try:
                os.makedirs(_DIR, exist_ok=True)
                if os.path.exists(_LOG) and os.path.getsize(_LOG) > _MAX_BYTES:
                    try:
                        os.replace(_LOG, _LOG + ".1")   # rotation : 1 backup, jamais perdu
                    except Exception:
                        pass
                with open(_LOG, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except Exception:
                pass
    except Exception:
        pass


def recent(limit=200):
    """Les N derniers événements (pour la tour de contrôle admin)."""
    out = []
    try:
        with open(_LOG, "r", encoding="utf-8") as f:
            for ln in f.readlines()[-int(limit):]:
                ln = ln.strip()
                if ln:
                    try:
                        out.append(json.loads(ln))
                    except Exception:
                        pass
    except Exception:
        pass
    return out


def summary(limit=4000):
    """Compte par code sur les derniers événements (tour de contrôle : vue d'ensemble)."""
    out = {}
    for e in recent(limit):
        c = e.get("code", "?")
        out[c] = out.get(c, 0) + 1
    return out
