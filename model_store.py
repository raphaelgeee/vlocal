#!/usr/bin/env python3
"""Vlocal — résolution + téléchargement des modèles hors-bundle (v3.2.8 — chantier B).

POURQUOI : sortir les ~2,8 Go de modèles Whisper du bundle pour que
  - l'app pèse ~150 Mo (au lieu de 2,8 Go),
  - les MISES À JOUR ne pèsent que le code (quelques Mo) — jamais re-DL du modèle,
  - le modèle soit téléchargé UNE seule fois (1er lancement) puis mis en cache.

RÈGLE DE SÉCURITÉ (ne JAMAIS casser la dictée) :
  models_dir() rend le dossier cache App Support S'IL EST COMPLET, SINON le bundle.
  -> Tant que la bascule n'est pas finie, un build qui bundle encore les modèles
     fonctionne EXACTEMENT comme avant. Migration sans aucun risque pour le cœur.

« COMPLET » veut dire vérifié, pas seulement présent. Le cache porte un état
(`state.json`) qui liste les fichiers attendus et, pour chacun, la taille et la
date de modification relevées juste après sa vérification SHA-256. Un modèle à
moitié téléchargé, tronqué ou réécrit depuis n'est donc jamais chargé :
models_dir() retombe sur le bundle et needs_download() redemande le
téléchargement, qui reprend là où il s'était arrêté. Les caches téléchargés avant
cet état gardent l'ancien contrôle (fichier sentinelle) : aucun utilisateur
existant ne retélécharge 2,8 Go.

Ce module porte aussi la primitive de téléchargement robuste (reprise par Range,
délai par lecture, vitesse nulle détectée, espace disque vérifié avant, .part
puis renommage atomique, taille exacte, SHA-256) qu'utilise aussi updater.py :
une seule implémentation pour les deux téléchargements de l'app.

La diarisation (41 Mo) reste bundlée : seuls les 3 modèles Whisper sont externalisés.
"""
import errno
import json
import os
import shutil
import sys
import time

SUPPORT = os.path.expanduser("~/Library/Application Support/Vlocal")
CACHE = os.path.join(SUPPORT, "models")

# Modèle -> fichier sentinelle prouvant la présence (contrôle du bundle, et des
# caches antérieurs à l'état vérifié).
REQUIRED = {
    "whisper-large-v3-turbo-mlx-q8": "config.json",    # GPU (primaire)
    "whisper-large-v3-turbo-int8":   "model.bin",      # CPU (repli)
    "whisper-small-int8":            "model.bin",       # CPU rapide
}

STATE_NAME = "state.json"

# Marge laissée libre en plus de ce qu'on écrit : macOS se comporte mal disque
# plein (swap, caches) et la dictée doit pouvoir continuer à écrire sa base.
DISK_MARGIN = 300 * 1024 * 1024


def _journal(code, **context):
    """Trace dans le journal structuré (errors.py). Ne lève jamais : un journal
    indisponible ne doit pas faire échouer une opération par ailleurs réussie."""
    try:
        import errors
        errors.log(code, **context)
    except Exception as e:  # noqa: BLE001
        print("[modèles] journal indisponible (%s) : %s %s" % (e, code, context))


def _bundle_models():
    """Dossier models/ DANS le bundle (présent tant que la bascule B n'est pas finie)."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "models")


def _read_state(root):
    """État vérifié du cache, ou None (absent ou illisible : on ne devine pas)."""
    try:
        with open(os.path.join(root, STATE_NAME), encoding="utf-8") as f:
            st = json.load(f)
        if isinstance(st, dict) and isinstance(st.get("expected"), dict):
            if not isinstance(st.get("verified"), dict):
                st["verified"] = {}
            return st
    except FileNotFoundError:
        return None
    except Exception as e:  # noqa: BLE001
        print("[modèles] état du cache illisible (%s) : il sera reconstruit." % e)
    return None


def _write_state(root, st):
    """Écrit l'état de façon atomique : un arrêt brutal laisse l'ancien ou le neuf."""
    os.makedirs(root, exist_ok=True)
    path = os.path.join(root, STATE_NAME)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _stamp(path):
    """Empreinte bon marché d'un fichier déjà vérifié : [taille, mtime en ns]."""
    st = os.stat(path)
    return [st.st_size, st.st_mtime_ns]


def _complete(root):
    """True si TOUS les modèles requis sont utilisables dans `root`.

    Avec un état vérifié : chaque fichier attendu doit exister avec la taille et
    la date relevées après sa vérification SHA-256 (un stat, aucun hachage au
    lancement). Sans état : le fichier sentinelle de chaque modèle suffit."""
    try:
        st = _read_state(root)
        if st is not None:
            for rel in st["expected"]:
                stamp = st["verified"].get(rel)
                if not stamp or _stamp(os.path.join(root, rel)) != list(stamp):
                    return False
            return all(any(rel.startswith(name + "/") for rel in st["expected"])
                       for name in REQUIRED)
        return all(os.path.exists(os.path.join(root, name, sentinel))
                   for name, sentinel in REQUIRED.items())
    except Exception:
        return False


def models_dir():
    """Dossier modèles à utiliser : cache App Support si COMPLET, SINON le bundle.
    Le repli bundle est le FILET : la dictée ne casse jamais pendant la bascule."""
    return CACHE if _complete(CACHE) else _bundle_models()


def needs_download():
    """True si les modèles ne sont NI en cache NI bundlés (app légère, tout 1er run)."""
    return not (_complete(CACHE) or _complete(_bundle_models()))


# --------------------------------------------------------------------------- #
# Primitive de téléchargement robuste (modèles ET mise à jour de l'app).
# --------------------------------------------------------------------------- #
class DownloadError(RuntimeError):
    """Échec de téléchargement, avec un code STABLE (message utilisateur, journal)
    et un détail technique. Hérite de RuntimeError : les appelants historiques
    qui attrapaient RuntimeError continuent de fonctionner.

    Codes : network, stalled, http, no_space, size_mismatch, checksum, manifest."""

    def __init__(self, code, detail=""):
        self.code = code
        self.detail = str(detail)
        super().__init__("%s: %s" % (code, detail) if detail else code)


# Cloudflare R2 (r2.dev) renvoie 403 sur l'UA "Python-urllib" par défaut -> UA explicite.
_UA = "Vlocal-Updater (macOS)"
_CHUNK = 1 << 20
READ_TIMEOUT = 30        # s sans le moindre octet reçu : la lecture échoue
STALL_WINDOW = 60        # s : fenêtre de mesure de la vitesse
STALL_MIN_BPS = 2048     # en dessous sur toute une fenêtre : connexion morte
ATTEMPTS = 5             # tentatives par fichier, chacune reprend la précédente
_FINAL = ("http", "no_space", "size_mismatch")   # réessayer n'y changerait rien

# Points d'injection des tests : ni réseau réel, ni attente réelle.
_now = time.monotonic
_sleep = time.sleep


def _open(url, timeout, headers=None):
    """urlopen avec User-Agent explicite (sinon Cloudflare R2 bloque en 403).
    `timeout` s'applique à CHAQUE opération du socket, pas au transfert entier."""
    import urllib.request
    h = {"User-Agent": _UA}
    h.update(headers or {})
    return urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout)


def free_bytes(path):
    """Octets libres sur le volume qui contient (ou contiendra) `path`."""
    p = os.path.abspath(path)
    while not os.path.exists(p):
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    return shutil.disk_usage(p).free


def ensure_free_space(path, need):
    """Lève DownloadError('no_space') si le volume de `path` n'a pas `need` octets
    libres en plus de la marge. Appelé AVANT d'écrire quoi que ce soit."""
    need = max(0, int(need)) + DISK_MARGIN
    free = free_bytes(path)
    if free < need:
        raise DownloadError("no_space", "libre %d o, requis %d o" % (free, need))


def _sha256(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def file_matches(path, size=None, sha256=None):
    """True si `path` existe avec la taille attendue et, si fournie, l'empreinte."""
    try:
        if size is not None and os.path.getsize(path) != int(size):
            return False
        return os.path.isfile(path) and (not sha256 or _sha256(path) == sha256.lower())
    except OSError:
        return False


def _remove(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def _content_range_start(resp):
    """Premier octet annoncé par Content-Range (« bytes 600-999/1000 »), ou None."""
    v = (resp.headers.get("Content-Range") or "").strip()
    try:
        return int(v.split()[1].split("-")[0])
    except (IndexError, ValueError):
        return None


def _one_pass(url, tmp, size, on_bytes):
    """Une tentative : ouvre (en reprise si un .part existe) et complète le .part.
    Rend la main quand le serveur a fini d'envoyer ; lève sinon."""
    import urllib.error
    have = os.path.getsize(tmp) if os.path.exists(tmp) else 0
    if size is not None and have > size:
        _remove(tmp)            # .part plus gros que le fichier attendu : il est faux
        have = 0
    if size is not None and have == size:
        return                  # tout est déjà là (coupure pendant la vérification)
    try:
        resp = _open(url, READ_TIMEOUT, {"Range": "bytes=%d-" % have} if have else None)
    except urllib.error.HTTPError as e:
        if e.code == 416 and have:
            _remove(tmp)        # reprise refusée : la tentative suivante repart de zéro
            raise DownloadError("network", "HTTP 416 sur reprise à %d o" % have)
        if 400 <= e.code < 500 and e.code not in (408, 429):
            raise DownloadError("http", "HTTP %d" % e.code)
        raise
    with resp:
        status = getattr(resp, "status", None) or resp.getcode()
        if have and (status != 206 or _content_range_start(resp) != have):
            have = 0            # le serveur ignore la reprise : on réécrit tout
        announced = resp.headers.get("Content-Length")
        announced = int(announced) if announced and announced.isdigit() else None
        if not have and size is not None and announced is not None and announced != size:
            raise DownloadError("size_mismatch", "annoncé %d o, attendu %d o" % (announced, size))
        if size is None and announced is not None:
            ensure_free_space(tmp, announced)
        with open(tmp, "ab" if have else "wb") as out:
            done = have
            win_t, win_bytes = _now(), 0
            while True:
                chunk = resp.read(_CHUNK)
                if not chunk:
                    break
                try:
                    out.write(chunk)
                except OSError as e:
                    if e.errno == errno.ENOSPC:
                        raise DownloadError("no_space", "disque plein pendant l'écriture")
                    raise
                done += len(chunk)
                if size is not None and done > size:
                    raise DownloadError("size_mismatch", "plus de %d o reçus" % size)
                if on_bytes:
                    on_bytes(done)
                win_bytes += len(chunk)
                t = _now()
                if t - win_t >= STALL_WINDOW:
                    if win_bytes < STALL_MIN_BPS * (t - win_t):
                        raise DownloadError("stalled", "%d o en %.0f s" % (win_bytes, t - win_t))
                    win_t, win_bytes = t, 0
            out.flush()
            os.fsync(out.fileno())


def fetch_resumable(url, dst, size=None, sha256=None, on_bytes=None, attempts=ATTEMPTS):
    """Télécharge `url` vers `dst` sans jamais exposer de fichier partiel.

    - écrit dans `dst + '.part'` et ne renomme (os.replace, atomique) qu'après
      contrôle de la taille exacte et, si fournie, de l'empreinte SHA-256 ;
    - REPRISE : un .part laissé par une coupure, ou par un lancement précédent,
      est complété par une requête Range depuis son dernier octet ;
    - chaque lecture a un délai (READ_TIMEOUT) ; une vitesse quasi nulle pendant
      STALL_WINDOW secondes abandonne la tentative, la suivante reprend ;
    - `attempts` tentatives avec attente croissante ; une erreur définitive
      (4xx, taille annoncée fausse, disque plein) ne réessaie pas.
    on_bytes(octets_reçus) optionnel. Renvoie `dst`. Lève DownloadError."""
    tmp = dst + ".part"
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    last = DownloadError("network", "aucune tentative")
    for i in range(max(1, attempts)):
        if i:
            _sleep(min(2 ** (i - 1), 15))
        try:
            _one_pass(url, tmp, size, on_bytes)
            got = os.path.getsize(tmp)
            if size is not None and got < size:
                last = DownloadError("network", "connexion fermée à %d/%d o" % (got, size))
                continue        # coupure propre côté serveur : la suivante reprend
            if sha256 and _sha256(tmp) != sha256.lower():
                _remove(tmp)    # contenu altéré : la suivante repart de zéro
                last = DownloadError("checksum", os.path.basename(dst))
                continue
            os.replace(tmp, dst)
            return dst
        except DownloadError as e:
            if e.code in _FINAL:
                if e.code == "size_mismatch":
                    _remove(tmp)
                raise
            last = e
        except Exception as e:  # noqa: BLE001 — réseau, socket, lecture : on reprend
            last = DownloadError("network", "%s: %s" % (type(e).__name__, e))
        print("[téléchargement] tentative %d/%d échouée : %s" % (i + 1, attempts, last))
    raise last


# --------------------------------------------------------------------------- #
# Téléchargement des modèles depuis R2 (1er lancement de l'app thin).
# --------------------------------------------------------------------------- #
_MANIFEST_URL = ("https://pub-4359855167d74cdca4c20e25f70b3c9a.r2.dev/"
                 "models_manifest.json")


def fetch_manifest(timeout=30):
    """Récupère le manifest des modèles depuis R2 (fichiers, taille, sha256)."""
    with _open(_MANIFEST_URL, timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _manifest_files(man):
    """(base_url, [(« modèle/fichier », taille, sha256 ou None)]) validés.
    Un manifest mal formé, ou dont un chemin sortirait du cache, est refusé."""
    try:
        out = []
        for model, files in man["models"].items():
            for rel, meta in files.items():
                path = "%s/%s" % (model, rel)
                parts = path.split("/")
                if ".." in parts or "" in parts or path == STATE_NAME:
                    raise ValueError("chemin refusé : %s" % path)
                sha = meta.get("sha256")
                out.append((path, int(meta["size"]), sha.lower() if sha else None))
        if not out:
            raise ValueError("aucun fichier")
        return man["base_url"].rstrip("/"), out
    except (KeyError, TypeError, AttributeError, ValueError) as e:
        raise DownloadError("manifest", e)


def download_models(progress=None, manifest=None):
    """Télécharge les modèles manquants de R2 vers le CACHE (App Support).

    - l'état attendu est écrit AVANT le premier octet : tant qu'un fichier n'est
      pas vérifié, le cache n'est pas « complet » et aucun modèle n'est chargé ;
    - un fichier vérifié et inchangé est sauté ; un fichier faux (taille ou SHA)
      est supprimé puis retéléchargé ; un .part est REPRIS par Range ;
    - l'espace disque est vérifié avant de commencer.
    progress(done, total, label) optionnel. Lève DownloadError (une RuntimeError)
    en cas d'échec définitif : l'appelant affiche un message clair + Réessayer."""
    man = manifest or fetch_manifest()
    base, files = _manifest_files(man)
    total = sum(size for _p, size, _s in files)

    st = _read_state(CACHE) or {"expected": {}, "verified": {}}
    expected = {p: [size, sha] for p, size, sha in files}
    if st["expected"] != expected:
        st = {"expected": expected,
              "verified": {p: v for p, v in st["verified"].items() if p in expected}}
        _write_state(CACHE, st)

    remaining = 0     # ce qu'il reste à recevoir ; un .part compte comme déjà reçu
    for path, size, _sha in files:
        dst = os.path.join(CACHE, path)
        if os.path.exists(dst) and os.path.getsize(dst) == size:
            continue
        part = dst + ".part"
        remaining += max(0, size - (os.path.getsize(part) if os.path.exists(part) else 0))
    try:
        ensure_free_space(CACHE, remaining)
    except DownloadError as e:
        _journal("disk_full", step="models", detail=e.detail)
        raise

    done = 0
    for path, size, sha in files:
        dst = os.path.join(CACHE, path)
        label = path.split("/", 1)[0]
        stamp = st["verified"].get(path)
        if stamp and os.path.exists(dst) and _stamp(dst) == list(stamp):
            pass                # vérifié lors d'un passage précédent, inchangé depuis
        elif file_matches(dst, size, sha):
            st["verified"][path] = _stamp(dst)
            _write_state(CACHE, st)
        else:
            if os.path.lexists(dst):
                print("[modèles] %s altéré (taille ou empreinte) : retéléchargement." % path)
                _journal("model_missing", step="corrupt", file=path)
                os.remove(dst)
            if st["verified"].pop(path, None) is not None:
                _write_state(CACHE, st)

            def _on_bytes(n, _base=done):
                if progress:
                    progress(_base + n, total, label)
            try:
                fetch_resumable("%s/%s" % (base, path), dst, size=size, sha256=sha,
                                on_bytes=_on_bytes)
            except DownloadError as e:
                _journal("model_missing", step="download", file=path,
                         reason=e.code, detail=e.detail)
                raise
            st["verified"][path] = _stamp(dst)
            _write_state(CACHE, st)
        done += size
        if progress:
            progress(done, total, label)
    return True
