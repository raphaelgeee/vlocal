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

La diarisation (41 Mo) reste bundlée : seuls les 3 modèles Whisper sont externalisés.
"""
import os
import sys

SUPPORT = os.path.expanduser("~/Library/Application Support/Vlocal")
CACHE = os.path.join(SUPPORT, "models")

# Modèle -> fichier sentinelle prouvant la présence (vérif fine = checksum au DL).
REQUIRED = {
    "whisper-large-v3-turbo-mlx-q8": "config.json",    # GPU (primaire)
    "whisper-large-v3-turbo-int8":   "model.bin",      # CPU (repli)
    "whisper-small-int8":            "model.bin",       # CPU rapide
}


def _bundle_models():
    """Dossier models/ DANS le bundle (présent tant que la bascule B n'est pas finie)."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "models")


def _complete(root):
    """True si TOUS les modèles requis ont leur fichier sentinelle dans `root`."""
    try:
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
# Téléchargement depuis R2 (1er lancement de l'app thin). Robuste : vérifié
# (sha256), atomique (.part -> rename), idempotent (reprise gratuite), réessais.
# --------------------------------------------------------------------------- #
_MANIFEST_URL = ("https://pub-4359855167d74cdca4c20e25f70b3c9a.r2.dev/"
                 "models_manifest.json")


def _sha256(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# Cloudflare R2 (r2.dev) renvoie 403 sur l'UA "Python-urllib" par défaut -> UA explicite.
_UA = "Vlocal-Updater (macOS)"


def _open(url, timeout):
    """urlopen avec User-Agent explicite (sinon Cloudflare R2 bloque en 403)."""
    import urllib.request
    return urllib.request.urlopen(
        urllib.request.Request(url, headers={"User-Agent": _UA}), timeout=timeout)


def fetch_manifest(timeout=30):
    """Récupère le manifest des modèles depuis R2 (liste fichiers + sha256)."""
    import json
    with _open(_MANIFEST_URL, timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def download_models(progress=None, manifest=None):
    """Télécharge les modèles manquants de R2 vers le CACHE (App Support).
    VÉRIFIÉ (sha256) + ATOMIQUE (.part puis os.replace) + IDEMPOTENT (un fichier
    déjà présent et au bon checksum est sauté = reprise gratuite après coupure)
    + 3 réessais par fichier. progress(done, total, label) optionnel. Lève en cas
    d'échec définitif (l'appelant affiche un message clair + bouton Réessayer)."""
    import urllib.request
    import os as _os
    man = manifest or fetch_manifest()
    base = man["base_url"]
    total = sum(f["size"] for m in man["models"].values() for f in m.values())
    done = 0
    _os.makedirs(CACHE, exist_ok=True)
    for model, files in man["models"].items():
        for rel, meta in files.items():
            dst = _os.path.join(CACHE, model, rel)
            if (_os.path.exists(dst) and _os.path.getsize(dst) == meta["size"]
                    and _sha256(dst) == meta["sha256"]):
                done += meta["size"]
                if progress:
                    progress(done, total, model)
                continue
            _os.makedirs(_os.path.dirname(dst), exist_ok=True)
            url = "%s/%s/%s" % (base, model, rel)
            tmp = dst + ".part"
            last_err = None
            for _attempt in range(3):
                try:
                    base_done = done
                    with _open(url, 60) as resp, open(tmp, "wb") as out:
                        while True:
                            chunk = resp.read(1 << 20)
                            if not chunk:
                                break
                            out.write(chunk)
                            if progress:
                                progress(base_done + out.tell(), total, model)
                    if _sha256(tmp) != meta["sha256"]:
                        raise ValueError("checksum %s" % rel)
                    _os.replace(tmp, dst)
                    done = base_done + meta["size"]
                    last_err = None
                    break
                except Exception as e:  # noqa: BLE001
                    last_err = e
                    try:
                        _os.remove(tmp)
                    except Exception:
                        pass
            if last_err is not None:
                raise RuntimeError("Téléchargement modèle échoué (%s/%s) : %s"
                                   % (model, rel, last_err))
    return True
