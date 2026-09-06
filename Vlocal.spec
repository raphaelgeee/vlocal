# -*- mode: python ; coding: utf-8 -*-
"""
Vlocal — spec PyInstaller (build .app macOS).

Embarque : code (100% local, déterministe) + interface HTML + assets +
  - Whisper large-v3-turbo int8 (moteur principal),
  - Whisper small int8 (dictée rapide / cascade),
  - v18 : modèles ONNX de diarisation (segmentation + embeddings) + sherpa-onnx.
Aucune dépendance réseau, aucun Ollama, aucun torch.

Build :  ./venv/bin/python -m PyInstaller --noconfirm Vlocal.spec
Sortie : dist/Vlocal.app
"""
import os
from PyInstaller.utils.hooks import collect_all

ROOT = os.path.abspath(os.getcwd())

# Version unique du produit : lue depuis le fichier VERSION à la racine
# (source partagée avec build_app.sh — éviter toute copie en dur).
with open(os.path.join(ROOT, "VERSION"), encoding="utf-8") as _f:
    VLOCAL_VERSION = _f.read().strip()

# --- Ressources embarquées (chemin source, dossier destination dans le bundle) ---
datas = [
    ("vlocal-interface.html", "."),
    ("dictation_overlay.html", "."),   # v19 — overlay de dictée (NSPanel + WKWebView)
    ("assets", "assets"),
    # v3.3 — APP THIN : les modèles Whisper CPU (turbo-int8 ~1,5 Go, small-int8
    # ~470 Mo) ne sont PLUS bundlés ici -> voir le bloc _THIN plus bas (DL R2 au
    # 1er lancement). Seule la diarisation reste embarquée (légère) :
    # v18 — modèles ONNX de diarisation (~30 Mo). Datas EXPLICITES : on
    # n'embarque que les fichiers lus au runtime (diarizer.py) + LICENSE,
    # pas les résidus du dossier source (archive, fp32, scripts upstream).
    # Sans eux, la réunion s'affiche en texte continu (repli silencieux).
    ("models/diarization/embedding.onnx", "models/diarization"),
    ("models/diarization/sherpa-onnx-pyannote-segmentation-3-0/model.int8.onnx",
     "models/diarization/sherpa-onnx-pyannote-segmentation-3-0"),
    ("models/diarization/sherpa-onnx-pyannote-segmentation-3-0/LICENSE",
     "models/diarization/sherpa-onnx-pyannote-segmentation-3-0"),
]
# v3.3 — APP THIN (défaut) : les 3 modèles Whisper (turbo-int8 ~1,5 Go, small-int8
# ~470 Mo, mlx-q8 GPU ~824 Mo) NE SONT PLUS bundlés -> app ~150 Mo, MAJ = code seul.
# Ils sont téléchargés de R2 au 1er lancement (model_store.py + écran « préparation »
# du dashboard). La diarisation (~30 Mo) reste bundlée. La sortie texte/qualité/RAM
# est INCHANGÉE (mêmes modèles, juste hors-bundle). REPLI hors-ligne :
#   VLOCAL_THIN=0 ./build_app.sh  -> ré-embarque les 3 modèles ; model_store.models_dir()
#   rend alors le bundle automatiquement (filet documenté dans model_store.py).
_THIN = os.environ.get("VLOCAL_THIN", "1") != "0"
if _THIN:
    print("[spec] build THIN — modèles Whisper téléchargés de R2 au 1er lancement.")
else:
    for _m in ("whisper-large-v3-turbo-int8", "whisper-small-int8",
               "whisper-large-v3-turbo-mlx-q8"):
        _p = os.path.join(ROOT, "models", _m)
        if os.path.isdir(_p):
            datas.append((_p, "models/" + _m))
            print(f"[spec] (VLOCAL_THIN=0) modèle bundlé : {_m}")
    print("[spec] build BUNDLÉ (VLOCAL_THIN=0) — app lourde, repli hors-ligne.")

binaries = []
hiddenimports = [
    "webview.platforms.cocoa",
    "visio_recorder",    # v1.0.12 — recorder visio (import paresseux) ; PAS meeting_visio (soundfile)
    # pynput : plus de hiddenimports forcés — il reste auto-collecté via
    # l'import d'inserter.py (repli collage Win/Linux) + le hook contrib.
    "psutil",
    "micworker",         # v1.0.22 — worker micro (process jetable) : chemin PRINCIPAL
                         # de la capture de dictée. Déclaré EXPLICITEMENT car engine.py
                         # l'importe dans un try/except (repli in-process) : on ne laisse
                         # pas son embarquement dépendre de l'analyse statique. Le
                         # smoke-test « worker micro gelé » du build vérifie qu'il répond.
    "sounddevice",       # v1.0.22 — seule dépendance du worker micro (process séparé)
    "diarizer",          # v18 — importé paresseusement dans app.py
    "voiceid",           # WhoTalks v1.2 — importé paresseusement (reconnaissance voix)
    "overlay",           # v19 — overlay de dictée (NSPanel + WKWebView)
    "updater",           # v1.0.5 — mise à jour in-app (download R2 + swap atomique)
    "permissions",       # v19 — détection permissions macOS (TCC)
    "hotkey_mac",        # v19 — raccourci global natif (NSEvent, sans pynput)
    "WebKit",            # v19 — WKWebView de l'overlay
    "UserNotifications",  # v1.0.2 — notifs natives UNUserNotificationCenter (rappels)
    "Quartz",            # v19 — CGEvent (collage inserter)
    "CoreFoundation",    # v19 — CFRunLoop pour le tap natif
    "ApplicationServices",  # v19 — AXIsProcessTrusted (Accessibilité)
    "mlx_engine",        # v23 — backend GPU MLX (repli CPU si absent)
    # Modules cœur listés explicitement (sûreté : certains ne sont importés que
    # dynamiquement ou depuis des sous-process --mic-worker / --emb-worker).
    "engine", "processor", "storage", "recorder", "live_meeting", "reminders",
]


# --- Paquets délicats : on récupère TOUT (data + dylibs + sous-modules) ---
# v18 — sherpa_onnx (+ son coeur natif) embarque des .dylib : collect_all est
# indispensable pour que la diarisation fonctionne dans le bundle.
for pkg in ("faster_whisper", "ctranslate2", "onnxruntime", "av",
            "sounddevice", "webview", "tokenizers", "sherpa_onnx",
            # v23 — GPU : mlx (libmlx.dylib + mlx.metallib via le hook intégré),
            # mlx_whisper (assets mel_filters/tiktoken), tiktoken (dépendance).
            "mlx", "mlx_whisper", "tiktoken", "tiktoken_ext",
            # v23 — dépendances RÉELLES de mlx_whisper (timing.py) : scipy.signal
            # + numba (+ llvmlite, backend LLVM de numba) pour l'alignement DTW
            # des word-timestamps. SANS elles, `import mlx_whisper` lève
            # ModuleNotFoundError -> available() False -> le GPU ne s'active
            # JAMAIS (repli CPU silencieux). C'était LE bug du build v23 initial.
            "scipy", "numba", "llvmlite"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception as _e:
        # mlx absent de l'environnement de build -> build CPU-only (repli auto
        # au runtime). On ne bloque pas le build.
        print(f"[spec] collect_all({pkg}) ignore : {_e}")

block_cipher = None

a = Analysis(
    ["app.py"],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # allègements : rien de tout ça n'est utilisé par l'app.
        # v23 — scipy N'EST PLUS exclu : mlx_whisper en dépend (timing.py).
        # torch/torchaudio RESTENT exclus : mlx_whisper/torch_whisper.py les
        # importe, mais ce module n'est PAS dans la chaîne de transcription
        # (vérifié : `import mlx_whisper` ne charge pas torch). Les exclure
        # économise plusieurs centaines de Mo sans rien casser.
        "tkinter", "matplotlib", "PIL", "pytest", "IPython",
        "pandas", "torch", "torchaudio",
    ],
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Vlocal",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,            # app fenêtrée (pas de terminal)
    disable_windowed_traceback=False,
    argv_emulation=True,      # ouverture par double-clic / fichiers déposés
    target_arch=None,
    codesign_identity=None,   # signature ad-hoc faite par build_app.sh
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Vlocal",
)
app = BUNDLE(
    coll,
    name="Vlocal.app",
    icon="build/AppIcon.icns",
    bundle_identifier="com.vlocal.app",
    info_plist={
        "CFBundleName": "Vlocal",
        "CFBundleDisplayName": "Vlocal",
        "CFBundleShortVersionString": VLOCAL_VERSION,
        "CFBundleVersion": VLOCAL_VERSION,
        "LSMinimumSystemVersion": "11.0",
        "NSHighResolutionCapable": True,
        "NSMicrophoneUsageDescription":
            "Vlocal utilise le microphone pour transcrire votre voix, "
            "uniquement en local sur votre Mac.",
        "NSAppleEventsUsageDescription":
            "Vlocal utilise AppleScript pour afficher ses notifications.",
    },
)
