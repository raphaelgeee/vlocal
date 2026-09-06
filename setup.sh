#!/bin/bash

echo "========================================"
echo "Vlocal — Installation backend vocal (Whisper)"
echo "========================================"
echo

# 1. Vérifier Python (3.9+ suffit pour faster-whisper)
echo "[1/3] Vérification Python..."
if ! command -v python3 &> /dev/null; then
    echo "❌ Python3 non trouvé !"
    echo "Installe Python 3.9+ : https://www.python.org/downloads/"
    exit 1
fi
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
    echo "❌ Python 3.9+ requis."
    exit 1
fi
echo "✅ Python3 trouvé ($(python3 -c 'import platform; print(platform.python_version())'))"
echo

# 2. Installer dependencies
echo "[2/3] Installation dependencies (requirements.txt)..."
pip3 install -r requirements.txt
if [ $? -ne 0 ]; then
    echo "❌ Erreur installation dependencies"
    exit 1
fi
echo "✅ Dependencies installées"
echo

# 3. Télécharger + tester
echo "[3/3] Téléchargement des modèles Whisper + test..."
python3 download_whisper.py && python3 app.py --selftest
if [ $? -ne 0 ]; then
    echo "❌ Erreur téléchargement / test"
    exit 1
fi
echo

echo "========================================"
echo "✅ INSTALLATION VLOCAL TERMINÉE !"
echo "========================================"
echo ""
echo "Le moteur vocal est prêt (Whisper large-v3-turbo + small, int8, CPU, français, 100% local)."
echo ""
echo "Commandes :"
echo "  python3 app.py                 # lancer l'app desktop (fenêtre + raccourci global)"
echo "  python3 app.py --selftest      # auto-test moteur (sans fenêtre/micro)"
echo "  python3 benchmark_whisper.py   # comparatif small vs large-v3-turbo"
echo "  python3 capture_micro.py       # dictée micro CLI (push-to-talk)"
echo ""
echo "Traitement 100% local, aucune donnée ne quitte le poste."
echo
