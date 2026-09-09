"""
Vlocal : télémétrie minimale et déclarée.

Ce que l'app envoie, et rien d'autre (voir aussi README, section « Données ») :
  - un identifiant d'installation aléatoire (UUID, généré au premier lancement) ;
  - le prénom, le nom et l'e-mail saisis par l'utilisateur à l'installation
    (modifiables dans Réglages, peuvent rester vides) ;
  - la version de Vlocal et la version de macOS ;
  - le modèle de Mac, la langue de l'interface, le raccourci choisi et le
    moteur utilisé (carte graphique ou processeur) ;
  - l'horodatage de la dernière dictée ;
  - par jour : nombre de dictées, de mots, durée de parole, temps gagné,
    réunions et mots de réunion ;
  - par jour, le nombre d'incidents techniques par code (micro indisponible,
    repli processeur...), sans aucun détail : de quoi voir si une installation
    va mal et proposer de l'aide.

Jamais : texte dicté, audio, noms de fichiers, contenu de réunions, nom de
machine, adresse IP côté client (Supabase voit l'IP de la requête comme tout
serveur HTTP ; elle n'est pas stockée par l'app).

Mécanique : un envoi au démarrage (après 60 s), puis toutes les 6 h, plus un
envoi différé de 10 min après une dictée. L'envoi est un appel RPC unique
(fonction `vlocal_report_usage`, la seule surface ouverte à la clé publique :
les tables elles-mêmes ne sont ni lisibles ni modifiables avec cette clé) qui
fait des upserts idempotents : rejouer un envoi ne compte jamais deux fois. Tout est best-effort : hors-ligne ou erreur, on
réessaie au prochain cycle, sans jamais gêner l'utilisateur.

Désactivable à tout moment (Réglages > Données partagées, ou
telemetry_enabled=false dans settings.json). Rien n'est envoyé tant que
l'utilisateur n'a pas fait son choix à l'installation.
"""
import json
import os
import re
import threading
import time
import uuid
import urllib.request

from supabase_config import auth_headers, rest_url

RPC_NAME = "vlocal_report_usage"
FIRST_SYNC_DELAY_S = 60.0
SYNC_PERIOD_S = 6 * 3600.0
USAGE_DEBOUNCE_S = 10 * 60.0
HTTP_TIMEOUT_S = 8.0
# v1.3.0 : 31 jours, et non 3. Les upserts sont idempotents, une trentaine de
# petites lignes ne coûtent rien, et cela répare les trous : une semaine hors
# ligne ou un Mac éteint ne perdait plus seulement l'envoi, mais les jours
# eux-mêmes. C'est exactement la borne acceptée par la fonction serveur.
DAYS_BACK = 31


_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s.]+(\.[^@\s.]+)+$")


def valid_email(value: str) -> bool:
    """Validation volontairement stricte et simple : une adresse mal formée est
    refusée à la saisie plutôt que stockée puis inexploitable."""
    v = (value or "").strip()
    return bool(v) and len(v) <= 200 and bool(_EMAIL_RE.match(v))


def new_install_id() -> str:
    return str(uuid.uuid4())


def is_enabled(settings: dict) -> bool:
    """True seulement si l'utilisateur a explicitement accepté."""
    return settings.get("telemetry_enabled") is True and bool(settings.get("install_id"))


EVENTS_LOG = os.path.expanduser("~/Library/Application Support/Vlocal/events.jsonl")


def read_incidents(since_day: str, path: str = None) -> list:
    """Agrège le journal local d'incidents (errors.py) par jour et par code.
    Ne transmet QUE des compteurs : jamais le contexte d'un événement, qui peut
    contenir un nom de périphérique ou un message. Ne lève jamais."""
    out = {}
    try:
        with open(path or EVENTS_LOG, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                day = (d.get("ts") or "")[:10]
                code = (d.get("code") or "").strip()[:40]
                if not code or not day or day < since_day:
                    continue
                out[(day, code)] = out.get((day, code), 0) + 1
    except Exception:
        return []
    return [{"day": d, "code": c, "count": n} for (d, c), n in sorted(out.items())][:200]


def build_rows(settings: dict, store, app_version: str, os_version: str,
               now: float = None, profile: dict = None) -> tuple:
    """Construit (ligne installs, lignes usage_days, compteurs d'incidents) à
    partir des réglages, des compteurs locaux et du journal d'événements.
    Pur : aucun réseau, testable."""
    now = now or time.time()
    email = (settings.get("email") or "").strip().lower()[:200]
    last_used = settings.get("last_used_at")
    profile = profile or {}
    install = {
        "install_id": settings.get("install_id"),
        "first_name": (settings.get("first_name") or "").strip()[:80],
        "last_name": (settings.get("last_name") or "").strip()[:80],
        "email": email if valid_email(email) else "",
        "app_version": str(app_version or "")[:32],
        "os_version": str(os_version or "")[:64],
        "last_seen_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now)),
        "last_used_at": (time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(float(last_used)))
                         if last_used else None),
        "mac_model": str(profile.get("mac_model") or "")[:64],
        "ui_lang": str(profile.get("ui_lang") or "")[:8],
        "hotkey": str(profile.get("hotkey") or "")[:24],
        "engine": str(profile.get("engine") or "")[:8],
    }
    since = time.strftime("%Y-%m-%d", time.localtime(now - (DAYS_BACK - 1) * 86400))
    usage = []
    for r in store.usage_days(since):
        usage.append({
            "install_id": install["install_id"],
            "day": r["day"],
            "dictations": int(r["dictations"]),
            "words": int(r["words"]),
            "seconds_saved": round(float(r["seconds_saved"]), 1),
            "audio_seconds": round(float(r.get("audio_seconds") or 0), 1),
            "meetings": int(r.get("meetings") or 0),
            "meeting_words": int(r.get("meeting_words") or 0),
        })
    return install, usage, read_incidents(since)


def _send(install: dict, usage: list, incidents: list = None) -> None:
    """Appel RPC PostgREST : POST /rest/v1/rpc/vlocal_report_usage."""
    body = json.dumps({
        "p_install_id": install["install_id"],
        "p_first_name": install["first_name"],
        "p_last_name": install["last_name"],
        "p_email": install["email"],
        "p_app_version": install["app_version"],
        "p_os_version": install["os_version"],
        "p_last_used_at": install["last_used_at"],
        "p_mac_model": install["mac_model"],
        "p_ui_lang": install["ui_lang"],
        "p_hotkey": install["hotkey"],
        "p_engine": install["engine"],
        "p_days": [{"day": u["day"], "dictations": u["dictations"], "words": u["words"],
                    "seconds_saved": u["seconds_saved"], "audio_seconds": u["audio_seconds"],
                    "meetings": u["meetings"], "meeting_words": u["meeting_words"]}
                   for u in usage],
        "p_incidents": incidents or [],
    }).encode("utf-8")
    req = urllib.request.Request(rest_url("rpc/" + RPC_NAME), data=body, method="POST")
    for k, v in auth_headers().items():
        req.add_header(k, v)
    req.add_header("Content-Type", "application/json")
    req.add_header("Prefer", "return=minimal")
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
        resp.read()


def sync_once(settings: dict, store, app_version: str, os_version: str,
              send=_send, profile: dict = None) -> bool:
    """Un envoi complet. Renvoie True si tout est parti. Ne lève jamais."""
    if not is_enabled(settings):
        return False
    try:
        install, usage, incidents = build_rows(settings, store, app_version,
                                               os_version, profile=profile)
        send(install, usage, incidents)
        return True
    except Exception as e:
        print(f"[telemetry] envoi différé : {e}")
        return False


class Telemetry:
    """Planificateur : un thread daemon, réveillable, qui appelle sync_once."""

    def __init__(self, load_settings, save_settings, get_store, app_version,
                 os_version, get_profile=None):
        self._load = load_settings
        self._save = save_settings
        self._store = get_store
        self._version = app_version
        self._os = os_version
        self._profile = get_profile or (lambda: {})
        self._wake = threading.Event()
        self._due_at = None
        self._thr = None
        self.last_sync = 0.0

    def start(self):
        if self._thr is not None:
            return
        self._due_at = time.time() + FIRST_SYNC_DELAY_S
        self._thr = threading.Thread(target=self._loop, name="telemetry",
                                     daemon=True)
        self._thr.start()

    def notify_usage(self):
        """Une dictée vient d'être comptée : envoi dans 10 min au plus."""
        soon = time.time() + USAGE_DEBOUNCE_S
        if self._due_at is None or soon < self._due_at:
            self._due_at = soon
        self._wake.set()

    def sync_now(self) -> bool:
        try:
            prof = self._profile()
        except Exception:
            prof = {}
        ok = sync_once(self._load(), self._store(), self._version, self._os, profile=prof)
        if ok:
            self.last_sync = time.time()
            try:
                self._save({"telemetry_last_sync": self.last_sync})
            except Exception:
                pass
        return ok

    def _loop(self):
        while True:
            wait = max(0.5, (self._due_at or time.time()) - time.time())
            self._wake.wait(wait)
            self._wake.clear()
            if time.time() < (self._due_at or 0):
                continue
            self._due_at = time.time() + SYNC_PERIOD_S
            store = self._store()
            if store is None:
                continue
            self.sync_now()
