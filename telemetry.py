"""
Vlocal : télémétrie minimale et déclarée.

Ce que l'app envoie, et rien d'autre (voir aussi README, section « Données ») :
  - un identifiant d'installation aléatoire (UUID, généré au premier lancement) ;
  - le prénom et le nom saisis par l'utilisateur à l'installation (modifiables
    dans Réglages, peuvent rester vides) ;
  - la version de Vlocal et la version de macOS ;
  - par jour : nombre de dictées, nombre de mots, temps gagné estimé.

Jamais : texte dicté, audio, noms de fichiers, contenu de réunions, adresse
e-mail, nom de machine, adresse IP côté client (Supabase voit l'IP de la
requête comme tout serveur HTTP ; elle n'est pas stockée par l'app).

Mécanique : un envoi au démarrage (après 60 s), puis toutes les 6 h, plus un
envoi différé de 10 min après une dictée. Les lignes sont des upserts
idempotents (PostgREST, `resolution=merge-duplicates`) : rejouer un envoi ne
compte jamais deux fois. Tout est best-effort : hors-ligne ou erreur, on
réessaie au prochain cycle, sans jamais gêner l'utilisateur.

Désactivable à tout moment (Réglages > Données partagées, ou
telemetry_enabled=false dans settings.json). Rien n'est envoyé tant que
l'utilisateur n'a pas fait son choix à l'installation.
"""
import json
import threading
import time
import uuid
import urllib.request

from supabase_config import auth_headers, rest_url

INSTALLS_TABLE = "installs"
USAGE_TABLE = "usage_days"
FIRST_SYNC_DELAY_S = 60.0
SYNC_PERIOD_S = 6 * 3600.0
USAGE_DEBOUNCE_S = 10 * 60.0
HTTP_TIMEOUT_S = 8.0
DAYS_BACK = 3          # on renvoie les 3 derniers jours (rattrape un envoi manqué)


def new_install_id() -> str:
    return str(uuid.uuid4())


def is_enabled(settings: dict) -> bool:
    """True seulement si l'utilisateur a explicitement accepté."""
    return settings.get("telemetry_enabled") is True and bool(settings.get("install_id"))


def build_rows(settings: dict, store, app_version: str, os_version: str,
               now: float = None) -> tuple:
    """Construit (ligne installs, lignes usage_days) à partir des réglages et
    des compteurs locaux. Pur : aucun réseau, testable."""
    now = now or time.time()
    install = {
        "install_id": settings.get("install_id"),
        "first_name": (settings.get("first_name") or "").strip()[:80],
        "last_name": (settings.get("last_name") or "").strip()[:80],
        "app_version": str(app_version or "")[:32],
        "os_version": str(os_version or "")[:64],
        "last_seen_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now)),
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
        })
    return install, usage


def _upsert(table: str, rows, on_conflict: str) -> None:
    body = json.dumps(rows).encode("utf-8")
    req = urllib.request.Request(
        rest_url(table) + "?on_conflict=" + on_conflict, data=body, method="POST")
    for k, v in auth_headers().items():
        req.add_header(k, v)
    req.add_header("Content-Type", "application/json")
    req.add_header("Prefer", "resolution=merge-duplicates,return=minimal")
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
        resp.read()


def sync_once(settings: dict, store, app_version: str, os_version: str,
              upsert=_upsert) -> bool:
    """Un envoi complet. Renvoie True si tout est parti. Ne lève jamais."""
    if not is_enabled(settings):
        return False
    try:
        install, usage = build_rows(settings, store, app_version, os_version)
        upsert(INSTALLS_TABLE, [install], "install_id")
        if usage:
            upsert(USAGE_TABLE, usage, "install_id,day")
        return True
    except Exception as e:
        print(f"[telemetry] envoi différé : {e}")
        return False


class Telemetry:
    """Planificateur : un thread daemon, réveillable, qui appelle sync_once."""

    def __init__(self, load_settings, save_settings, get_store, app_version,
                 os_version):
        self._load = load_settings
        self._save = save_settings
        self._store = get_store
        self._version = app_version
        self._os = os_version
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
        ok = sync_once(self._load(), self._store(), self._version, self._os)
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
