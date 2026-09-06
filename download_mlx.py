#!/usr/bin/env python3
"""
Vlocal — Récupère le modèle Whisper large-v3-turbo au format MLX (GPU Apple) et
le copie dans models/whisper-large-v3-turbo-mlx/ pour l'EMBARQUER dans le .app
(100 % local : aucun téléchargement au runtime).

À lancer UNE fois avant le build (outil de dev, comme download_whisper.py) :
    ./venv/bin/python download_mlx.py

Stratégie : on déréférence le snapshot du cache HuggingFace s'il est déjà
présent (cas du dev qui a prototypé), sinon on télécharge via huggingface_hub.
Les symlinks du cache sont COPIÉS en vrais fichiers (le bundle ne suit pas les
liens). ~1,5 Go.
"""
import os
import shutil
import sys

REPO = "mlx-community/whisper-large-v3-turbo"
DEST = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                    "models", "whisper-large-v3-turbo-mlx")
# Fichiers réellement lus par mlx_whisper au chargement.
NEEDED = ("config.json", "weights.safetensors")


def _from_cache():
    """Renvoie le dossier snapshot du cache HF s'il contient les fichiers."""
    base = os.path.expanduser(
        "~/.cache/huggingface/hub/models--mlx-community--whisper-large-v3-turbo/snapshots")
    if not os.path.isdir(base):
        return None
    for snap in sorted(os.listdir(base), reverse=True):
        d = os.path.join(base, snap)
        if all(os.path.exists(os.path.join(d, f)) for f in NEEDED):
            return d
    return None


def main():
    os.makedirs(DEST, exist_ok=True)
    src = _from_cache()
    if src is None:
        print("Pas en cache -> téléchargement via huggingface_hub...")
        from huggingface_hub import snapshot_download
        src = snapshot_download(REPO, allow_patterns=list(NEEDED) + ["*.json"])
    print(f"Source : {src}")
    for f in NEEDED:
        s = os.path.join(src, f)
        d = os.path.join(DEST, f)
        # copyfile suit les symlinks -> écrit un VRAI fichier dans models/.
        shutil.copyfile(s, d)
        print(f"  copié {f} ({os.path.getsize(d) / 1e6:.0f} Mo)")
    # config supplémentaires éventuelles (tokenizer, etc.)
    for f in os.listdir(src):
        if f.endswith(".json") and not os.path.exists(os.path.join(DEST, f)):
            shutil.copyfile(os.path.join(src, f), os.path.join(DEST, f))
    print(f"\nModèle MLX prêt dans {DEST}")
    # Vérification de chargement réel.
    try:
        import mlx_whisper
        import numpy as np
        mlx_whisper.transcribe(np.zeros(8000, dtype=np.float32),
                               path_or_hf_repo=DEST, language="fr")
        print("Chargement MLX depuis le dossier bundlé : OK")
    except Exception as e:
        print(f"AVERTISSEMENT : test de chargement KO : {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
