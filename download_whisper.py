#!/usr/bin/env python3
"""
Vlocal — Téléchargement propre des modèles Whisper (faster-whisper / CTranslate2)

Backend vocal CPU-only. Télécharge les conversions CTranslate2 officielles dans
un dossier local du projet (models/), en layout plat, pour un déploiement maîtrisé.

Les dépôts sont résolus depuis le mapping interne de faster-whisper (source la
mieux maintenue), pas codés en dur :
  - small          -> Systran/faster-whisper-small
  - large-v3-turbo -> mobiuslabsgmbh/faster-whisper-large-v3-turbo

NB : la quantisation int8 est appliquée au CHARGEMENT (compute_type="int8").
Le poids sur disque correspond à la précision stockée par le dépôt ; la taille
réelle est mesurée et affichée après téléchargement (aucun chiffre inventé).
"""

import sys
from pathlib import Path

# NB : _MODELS est une API privée de faster-whisper — peut casser en montée de
# version. Garde explicite pour éviter un NameError plus loin (_MODELS[name]).
try:
    from faster_whisper.utils import _MODELS
except ImportError as e:
    print(
        "ERREUR : faster_whisper.utils._MODELS introuvable (API privée, "
        f"a pu disparaître lors d'une montée de version de faster-whisper) : {e}",
        file=sys.stderr,
    )
    sys.exit(1)

# Modèles à récupérer : nom faster-whisper -> dossier local
MODELS = {
    "small": "models/whisper-small-int8",
    "large-v3-turbo": "models/whisper-large-v3-turbo-int8",
}


def dir_size_mb(path: Path) -> float:
    total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    return total / (1024 * 1024)


def download_one(name: str, local_dir: str) -> bool:
    from huggingface_hub import snapshot_download

    repo_id = _MODELS[name]
    out = Path(local_dir)
    print(f"📥 {name}  ←  {repo_id}")
    print(f"   Destination : {out}/")
    try:
        snapshot_download(
            repo_id=repo_id,
            local_dir=str(out),
            # On ne récupère que ce dont CTranslate2 a besoin (pas les .pt/.safetensors)
            allow_patterns=[
                "model.bin",
                "config.json",
                "tokenizer.json",
                "vocabulary.*",
                "preprocessor_config.json",
            ],
        )
    except Exception as e:
        print(f"   ❌ Échec téléchargement : {type(e).__name__}: {e}")
        return False

    model_bin = out / "model.bin"
    if not model_bin.exists() or model_bin.stat().st_size < 1_000_000:
        print("   ❌ model.bin absent ou trop petit (téléchargement corrompu ?)")
        return False

    size = dir_size_mb(out)
    print(f"   ✅ Fichiers présents — taille sur disque : {size:.1f} Mo")

    # Vérification que CTranslate2 charge bien le modèle en int8 depuis le dossier local
    try:
        from faster_whisper import WhisperModel

        print("   ⏳ Vérification de chargement (compute_type=int8)...")
        WhisperModel(str(out), device="cpu", compute_type="int8")
        print("   ✅ Modèle chargeable en int8 sur CPU.")
    except Exception as e:
        print(f"   ❌ Modèle non chargeable : {type(e).__name__}: {e}")
        return False

    print()
    return True


def main():
    print("=" * 60)
    print("Vlocal — Téléchargement modèles Whisper (faster-whisper)")
    print("=" * 60)
    print()
    print("📜 Source : conversions CTranslate2 officielles (HuggingFace)")
    print("   Licence Whisper : MIT (OpenAI). Traitement 100% local.")
    print()

    Path("models").mkdir(exist_ok=True)

    ok = True
    for name, local_dir in MODELS.items():
        if not download_one(name, local_dir):
            ok = False

    print("=" * 60)
    if ok:
        print("✅ SUCCÈS : tous les modèles sont téléchargés et chargeables.")
        print("   Prochain step : python3 app.py --selftest")
        sys.exit(0)
    else:
        print("❌ ÉCHEC : au moins un modèle manque ou est corrompu.")
        print("   Relance : python download_whisper.py")
        sys.exit(1)


if __name__ == "__main__":
    main()
