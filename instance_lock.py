"""Verrou d'instance unique de Vlocal.

Deux instances, c'est deux raccourcis globaux, deux bulles de dictée et le même
texte inséré deux fois. Le verrou tient en deux couches, chacune un fichier
verrouillé par flock, exclusif et non bloquant :

  1. dans le dossier de support de l'app, qui suit $HOME ;
  2. dans le dossier temporaire PRIVÉ de l'utilisateur (celui que macOS attribue
     par compte, indépendant de $HOME, accessible à lui seul). Deux instances
     lancées avec des HOME différents se voient quand même. Cas vécu le
     11 septembre 2026 : une instance de test lancée en HOME isolé a tourné une
     heure à côté de la vraie, et l'utilisateur avait deux bulles.

Seul EWOULDBLOCK veut dire « une autre instance tient le verrou ». Toute autre
erreur (système de fichiers réseau sans flock, dossier indisponible) ne bloque
JAMAIS le démarrage : mieux vaut un doublon improbable qu'une app qui refuse de
s'ouvrir. Un HOME sur un partage réseau produisait exactement cette panne avec
l'ancien code, qui confondait « verrouillé » et « impossible à verrouiller ».

VLOCAL_INSTANCE_SCOPE=home : couche 1 seulement. Réservé aux bancs d'essai qui
tournent volontairement en HOME isolé à côté de l'instance de l'utilisateur.
"""
import errno
import os

_CS_DARWIN_USER_TEMP_DIR = 65537   # <unistd.h>, valeur stable depuis Mac OS X 10.4


def user_private_dir():
    """Le dossier temporaire par utilisateur de macOS (/var/folders/.../T/),
    indépendant de $HOME et réservé à lui. None si on ne peut pas s'en assurer :
    on ne pose jamais un verrou dans un dossier que d'autres pourraient tenir."""
    chemin = None
    try:
        import ctypes
        import ctypes.util
        libc = ctypes.CDLL(ctypes.util.find_library("c"))
        buf = ctypes.create_string_buffer(1024)
        if libc.confstr(_CS_DARWIN_USER_TEMP_DIR, buf, 1024) > 0:
            chemin = buf.value.decode("utf-8", "replace")
    except Exception:
        chemin = None
    if not chemin:
        chemin = os.environ.get("TMPDIR")
    if not chemin:
        return None
    try:
        st = os.stat(chemin)
        if st.st_uid != os.getuid() or (st.st_mode & 0o077):
            return None       # pas à nous seuls : un autre compte pourrait bloquer Vlocal
    except Exception:
        return None
    return chemin


def _try_lock(path):
    """Renvoie (handle, tenu_par_un_autre). handle vaut None si le verrou n'a pas
    pu être posé pour une raison qui n'est PAS une autre instance."""
    try:
        import fcntl
        os.makedirs(os.path.dirname(path), exist_ok=True)
        f = open(path, "a")               # 'a' : n'écrase rien, ne tronque rien
    except Exception:
        return None, False
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f, False
    except OSError as e:
        f.close()
        return None, e.errno in (errno.EWOULDBLOCK, errno.EAGAIN)
    except Exception:
        f.close()
        return None, False


def acquire(support_dir, scope=None, user_lock_path=None):
    """Prend le verrou d'instance.

    Renvoie la liste des handles à GARDER VIVANTS tant que l'app tourne (les
    fermer relâche le verrou), ou None si une autre instance tourne déjà.
    `user_lock_path` sert aux tests ; par défaut le fichier vit dans
    user_private_dir()."""
    scope = scope or os.environ.get("VLOCAL_INSTANCE_SCOPE", "all")
    handles = []

    h, autre = _try_lock(os.path.join(support_dir, "vlocal.lock"))
    if autre:
        return None
    if h is not None:
        handles.append(h)

    if scope != "home":
        if user_lock_path is None:
            d = user_private_dir()
            user_lock_path = os.path.join(d, "vlocal-%d.lock" % os.getuid()) if d else None
        if user_lock_path:
            h, autre = _try_lock(user_lock_path)
            if autre:
                for x in handles:
                    x.close()
                return None
            if h is not None:
                handles.append(h)
    return handles
