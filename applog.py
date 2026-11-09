"""Journal de Vlocal (module logging de la bibliothèque standard).

Chaque module écrit dans son logger, `logging.getLogger("vlocal.<module>")`.
La configuration est posée une seule fois, par app.main(), via setup() :

- app empaquetée : fichier tournant ~/Library/Logs/Vlocal/vlocal.log
  (1 Mo, 5 fichiers gardés), niveau INFO. stdout et stderr sont redirigés vers
  console.log dans le même dossier, tronqué à chaque démarrage : il reçoit les
  messages natifs (AppKit, PortAudio) et ceux des modules qui écrivent encore
  avec print(). Sans cette redirection, une app lancée par le Finder perd
  tout ce qui sort sur la console ;
- développement (python app.py) : la console, au même niveau.

Les tests n'appellent jamais setup() : aucun fichier n'est écrit, et seuls les
avertissements remontent sur stderr (gestionnaire de dernier recours de Python).
Les échecs ignorés volontairement sont journalisés en DEBUG avec leur trace :
invisibles au niveau INFO, disponibles en passant VLOCAL_LOG_LEVEL=DEBUG.
"""
import logging
import logging.handlers
import os
import sys

LOG_DIR = os.path.expanduser("~/Library/Logs/Vlocal")
LOG_NAME = "vlocal.log"
CONSOLE_NAME = "console.log"
MAX_BYTES = 1_000_000
BACKUPS = 5
FORMAT = "%(asctime)s %(levelname)s %(name)s.%(funcName)s: %(message)s"

ROOT = "vlocal"
_configured = False


def get(module_name):
    """Logger d'un module de l'app : vlocal.<module>."""
    return logging.getLogger(f"{ROOT}.{module_name}")


def setup(frozen=None, log_dir=LOG_DIR, level=None):
    """Configure le journal une fois pour tout le processus (idempotent).
    frozen : app empaquetée (défaut : sys.frozen). Renvoie le chemin du journal,
    ou None en développement (console)."""
    global _configured
    if _configured:
        return None
    _configured = True
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    level = level or os.environ.get("VLOCAL_LOG_LEVEL", "INFO").upper()
    root = logging.getLogger(ROOT)
    root.setLevel(getattr(logging, level, logging.INFO))
    root.propagate = False
    if not frozen:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(FORMAT))
        root.addHandler(handler)
        return None
    try:
        os.makedirs(log_dir, exist_ok=True)
        path = os.path.join(log_dir, LOG_NAME)
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8")
    except OSError as e:
        # Dossier des journaux inaccessible : on garde la console, et on le dit.
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(FORMAT))
        root.addHandler(handler)
        root.warning("journal sur disque indisponible (%s) : console seulement", e)
        return None
    handler.setFormatter(logging.Formatter(FORMAT))
    root.addHandler(handler)
    _redirect_console(os.path.join(log_dir, CONSOLE_NAME), root)
    root.info("Vlocal démarre, journal de diagnostic actif.")
    return path


def _redirect_console(path, root):
    """Descripteurs 1 et 2 vers console.log (tronqué à chaque démarrage), en
    tampon ligne : les messages natifs et les print() restants y arrivent dans
    l'ordre."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        os.dup2(fd, 1)
        os.dup2(fd, 2)
        os.close(fd)
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except (OSError, AttributeError, ValueError) as e:
        # AttributeError/ValueError : flux remplacés ou fermés (sans reconfigure).
        root.warning("redirection de la console impossible : %s", e)
