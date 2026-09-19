#!/bin/bash
# ============================================================================
# Vlocal — build du .app téléchargeable (version légère, gratuite).
#
#   ./build_app.sh
#
# Produit :
#   dist/Vlocal.app          (l'application)
#   dist/Vlocal-<version>.dmg  (image disque à envoyer / déposer sur un Drive ;
#                               version lue depuis le fichier VERSION à la racine)
#
# Workflow : tu continues à développer avec `./venv/bin/python app.py`.
# Quand tu veux livrer une version, tu lances ce script -> nouveau .app + .dmg.
# ============================================================================
set -e
cd "$(dirname "$0")"
PY=./venv/bin/python
# Version unique : lue depuis le fichier VERSION à la racine (partagé avec Vlocal.spec).
VERSION="$(cat VERSION)"
APP="dist/Vlocal.app"
DMG="dist/Vlocal-${VERSION}.dmg"

# v23.1 — patch RAM : rendre l'import numba/scipy de mlx_whisper paresseux
# (-113 Mo en dictée). Idempotent ; sans effet si déjà appliqué ou si mlx_whisper
# absent (build CPU-only). numba/scipy/llvmlite restent bundlés (collect_all du
# spec) -> les réunions (word-timestamps) chargent timing.py à la demande.
$PY patch_mlx_whisper.py 2>/dev/null || echo "  (patch mlx_whisper ignoré : absent ou déjà fait)"

# ----------------------------------------------------------------------------
# VERROU DE NON-RÉGRESSION (qualité + vitesse + RAM) — étape BLOQUANTE.
# Compare le build courant à la baseline figée (tests/rd/gate_baseline.json).
# Tout franchissement de seuil -> exit 1 : AUCUN build ne sort dégradé.
# On verrouille le backend GPU nominal (rapide). Le backend CPU/Intel se vérifie
# séparément (plus lent) AVANT un build x86_64 ou en contrôle périodique :
#   VLOCAL_NO_MLX=1 $PY tests/rd/regression_gate.py --backend cpu_int8
# (Si la baseline doit évoluer après une amélioration VALIDÉE : --freeze.)
# ----------------------------------------------------------------------------
# VLOCAL_SKIP_GATES=1 : saute les verrous moteur (qui chargent les modèles = gros
# pic RAM). À n'utiliser QUE pour une modif sans impact moteur (ex. UI/activation),
# sur une machine à RAM serrée. Les smoke-tests d'empaquetage (3bis) restent actifs.
if [ -n "${VLOCAL_SKIP_GATES:-}" ]; then
  echo "==> 0/6  Verrous non-régression SAUTÉS (VLOCAL_SKIP_GATES=1 — modif UI seulement, moteur inchangé)"
else
echo "==> 0/6  Verrou non-régression qualité+vitesse+RAM (GPU)"
$PY tests/rd/regression_gate.py --backend mlx_q8 \
  || { echo "  >>> RÉGRESSION détectée — build STOPPÉ (corriger ou re-geler la baseline)."; exit 1; }

echo "==> 0bis  Verrou RÉUNION (RAM<=1,6Go + vitesse + DER, pipeline complet)"
$PY tests/rd/reunion_gate.py \
  || { echo "  >>> RÉGRESSION RÉUNION détectée — build STOPPÉ."; exit 1; }

# v29.3.1 — SMOKE-TEST de l'ORCHESTRATION du pipeline réunion LIVE (app.py), que
# les verrous moteur ne couvrent pas. Le bug _dur_min (UnboundLocalError en
# branche live) avait atteint l'utilisateur faute de ce test. Désormais : toute
# erreur d'orchestration du chemin live STOPPE le build.
echo "==> 0ter  Smoke-test PIPELINE RÉUNION LIVE (orchestration)"
$PY tests/rd/reunion_live_smoke.py \
  || { echo "  >>> PIPELINE RÉUNION LIVE KO — build STOPPÉ."; exit 1; }
fi

echo "==> 1/6  Nettoyage"
rm -rf build/Vlocal dist/Vlocal.app dist/Vlocal "$DMG" 2>/dev/null || true
# v29.2 — dist/ est sous ~/Desktop (iCloud) : SANS purge, chaque build empilait
# un DMG + des copies-conflit iCloud (« Vlocal N.app ») -> vu 70 Go uploadés par
# cloudd à 99 %, ce qui ÉTRANGLAIT la machine (dictée affamée, figée). On purge
# donc TOUS les anciens artefacts régénérables à chaque build : dist/ reste borné
# (~6 Go = app + DMG courants), iCloud ne suffoque plus.
rm -rf dist/Vlocal\ *.app dist/_old_builds 2>/dev/null || true
rm -f  dist/Vlocal-*.dmg dist/Vlocal\ *.dmg 2>/dev/null || true

echo "==> 1bis  Cohérence i18n du dashboard (fr/en alignés, aucune clé manquante)"
$PY tests/check_i18n.py || { echo "  >>> i18n incohérent, build STOPPÉ."; exit 1; }

echo "==> 1ter  Cohérence des versions (fichier VERSION = APP_VERSION = CHANGELOG)"
$PY -m unittest tests.test_version_sync -q >/dev/null 2>&1 \
  || { echo "  >>> VERSION et APP_VERSION divergent (le DMG serait mal nommé), build STOPPÉ."; exit 1; }

# ----------------------------------------------------------------------------
# v1.3.2 — TROIS PORTES qui auraient arrêté la 1.2.0 et la 1.3.0. Ces deux
# versions levaient une UnboundLocalError à CHAQUE lancement dans
# start_global_hotkey() : plus de raccourci global, et impossible de rouvrir ou
# de quitter Vlocal sans forcer. Le build passait, rien ne le signalait.
# Aucune de ces portes ne charge le moteur : elles tournent toujours, y compris
# avec VLOCAL_SKIP_GATES.
# ----------------------------------------------------------------------------
echo "==> 1quater  Analyse statique : noms non définis ou utilisés avant affectation"
BAD_NAMES="$($PY -m pyflakes *.py 2>&1 | grep -E "undefined name|referenced before assignment" || true)"
if [ -n "$BAD_NAMES" ]; then
  echo "$BAD_NAMES"
  echo "  >>> un nom sera introuvable à l'exécution, build STOPPÉ."; exit 1
fi

echo "==> 1quinquies  Tests unitaires (suite complète)"
$PY -m unittest discover -s tests -p "test_*.py" -q >/tmp/vlocal_unittest.log 2>&1 \
  || { tail -30 /tmp/vlocal_unittest.log; echo "  >>> tests unitaires KO, build STOPPÉ."; exit 1; }

echo "==> 1sexies  Cycle de vie de la VRAIE app : raccourci, fermer, rouvrir, quitter"
$PY tests/lifecycle_smoke.py \
  || { echo "  >>> cycle de vie KO (raccourci, rouvrir ou quitter), build STOPPÉ."; exit 1; }

echo "==> 2/6  Icône (V sur charbon)"
$PY tools/make_icon.py

echo "==> 3/6  Empaquetage PyInstaller (peut prendre 1-3 min)"
$PY -m PyInstaller --noconfirm --clean Vlocal.spec
# Restaure immédiatement les .py source après l'empaquetage (dev = .py lisibles).

if [ ! -d "$APP" ]; then
  echo "ERREUR : $APP n'a pas été créé."; exit 1
fi

# v1.0.25 — LANCEUR DES SOUS-PROCESS, HORS Contents/MacOS (anti-icône Dock).
# macOS enregistre comme APPLICATION tout binaire lancé depuis Contents/MacOS :
# chaque worker d'analyse obtenait donc sa propre icône, qui clignotait à chaque
# lot (une douzaine sur une réunion d'une heure). Depuis Contents/Helpers, le
# même binaire démarre en « prohibited » : jamais d'icône. Mesuré avant/après.
#   - lien SYMBOLIQUE (et non un lien dur) : un lien dur est dupliqué par
#     hdiutil dans le DMG (+36 Mo de téléchargement pour rien) ; le symlink
#     donne exactement le même résultat, vérifié (politique « prohibited ») ;
#   - _internal -> ../Frameworks : PyInstaller cherche ses bibliothèques à côté
#     du binaire, or le chemin spécial « Contents/Frameworks » n'est appliqué
#     que pour Contents/MacOS. Sans ce lien, le worker ne démarre pas.
# Créés AVANT la signature pour être couverts par elle.
echo "==> 3quater  Lanceur des sous-process (Contents/Helpers, sans icône Dock)"
mkdir -p "$APP/Contents/Helpers"
rm -f "$APP/Contents/Helpers/VlocalWorker" "$APP/Contents/Helpers/_internal"
if ln -s ../MacOS/Vlocal "$APP/Contents/Helpers/VlocalWorker" 2>/dev/null \
   && ln -s ../Frameworks "$APP/Contents/Helpers/_internal"; then
  echo "    ✅ Contents/Helpers/VlocalWorker (+ _internal -> ../Frameworks)"
else
  echo "    ⚠️ lanceur non créé -> les workers repartiront depuis MacOS/ (icône Dock)"
fi

echo "==> 3bis  Smoke-test du WORKER GELÉ (--emb-worker, AVANT signature)"
# Le re-exec du binaire gelé est le chemin RAM critique de la diarisation
# réunion (extraction sous-process). S'il casse au freeze (rthooks, imports),
# chaque réunion replierait en silence sur le chemin in-process plafonné.
# On l'exécute ici sur un mini-WAV : échec = build STOPPÉ.
$PY - <<'PYEOF' || { echo "  >>> WORKER GELÉ KO — build STOPPÉ."; exit 1; }
import json, os, subprocess, sys, tempfile, wave
import numpy as np
sr = 16000
t = np.arange(sr * 8, dtype=np.float32) / sr
sig = (0.2 * np.sin(2 * np.pi * 220 * t) * (1 + 0.3 * np.sin(2 * np.pi * 3 * t)))
pcm = (sig * 32767).astype("<i2")
wav = tempfile.mktemp(suffix=".wav")
with wave.open(wav, "wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
    w.writeframes(pcm.tobytes())
job = tempfile.mktemp(suffix=".json")
out = job + ".npz"
json.dump({"wav": wav, "sr": sr, "spans": [[0.5, 3.5], [4.0, 7.0]],
           "mode": "region"}, open(job, "w"))
# v1.0.25 — on teste le chemin RÉELLEMENT utilisé au runtime
# (Contents/Helpers, sans icône Dock), pas Contents/MacOS.
import os as _os
_exe = "dist/Vlocal.app/Contents/Helpers/VlocalWorker"
if not _os.path.exists(_exe):
    print("  >>> lanceur Helpers ABSENT — les workers auraient une icône Dock.")
    sys.exit(1)
r = subprocess.run([_exe, "--emb-worker", job, out],
                   capture_output=True, timeout=180)
ok = r.returncode == 0 and os.path.exists(out)
if ok:
    z = np.load(out)
    ok = z["E"].shape[1] == 192 and len(z["E"]) >= 1
print("  worker gelé :", "OK" if ok else
      f"KO rc={r.returncode} stderr={r.stderr[-200:]!r}")
for p in (wav, job, out):
    os.path.exists(p) and os.remove(p)
sys.exit(0 if ok else 1)
PYEOF

echo "==> 3bis-2  Smoke-test du WORKER MICRO GELÉ (--mic-worker, AVANT signature)"
# v1.0.22 — MICRO BÉTON : la capture de dictée passe par ce worker. S'il casse
# au freeze (import micworker manquant, rthooks), CHAQUE dictée replierait en
# silence sur le chemin in-process (celui qui se corrompt) -> toute la MAJ ne
# servirait à rien. On vérifie ready + ping/pong + quit propre, SANS ouvrir le
# micro (zéro prompt TCC en build). Échec = build STOPPÉ.
$PY - <<'PYEOF' || { echo "  >>> WORKER MICRO GELÉ KO — build STOPPÉ."; exit 1; }
import json, struct, subprocess, sys, time
p = subprocess.Popen(["dist/Vlocal.app/Contents/Helpers/VlocalWorker", "--mic-worker"],
                     stdin=subprocess.PIPE, stdout=subprocess.PIPE)
def read_ev(timeout=15.0):
    # messages cadrés : 1 octet type + longueur uint32 BE + payload
    head = p.stdout.read(5)
    assert head and len(head) == 5, "worker muet"
    typ, ln = head[:1], struct.unpack(">I", head[1:])[0]
    payload = p.stdout.read(ln)
    assert typ == b"J", f"type inattendu {typ!r}"
    return json.loads(payload.decode("utf-8"))
try:
    ev = read_ev()
    assert ev.get("ev") == "ready", f"pas ready : {ev}"
    p.stdin.write(b'{"cmd":"ping"}\n'); p.stdin.flush()
    ev = read_ev()
    assert ev.get("ev") == "pong", f"pas pong : {ev}"
    p.stdin.write(b'{"cmd":"quit"}\n'); p.stdin.flush()
    rc = p.wait(timeout=10)
    print("  worker micro gelé : OK (ready + pong + quit rc=%s)" % rc)
    sys.exit(0)
except Exception as e:
    print(f"  worker micro gelé : KO {e}")
    p.kill()
    sys.exit(1)
PYEOF

echo "==> 3ter  GARDE MLX minos<=14 + metallib macosx<=14 (anti-régression cross-device)"
# v3.2.8 — Empêche À TOUT JAMAIS de re-livrer un core MLX ciblé macOS 26 (ne charge
# pas sous macOS 14/15 -> GPU KO -> repli CPU lent chez la majorité des clients).
# Léger (otool/strings, pas de modèle chargé) : tourne MÊME en VLOCAL_SKIP_GATES.
$PY - <<'PYEOF' || { echo "  >>> MLX MAL CIBLÉ — build STOPPÉ. Réinstalle le wheel macOS 14 : voir mémoire vlocal-mlx-metallib-target."; exit 1; }
import glob, subprocess, sys, re
sp = "venv/lib/python3.12/site-packages"
so = glob.glob(f"{sp}/mlx/core.cpython-*-darwin.so")
if not so:
    print("  (mlx core absent -> build CPU-only, garde non applicable)"); sys.exit(0)
lc = subprocess.check_output(["otool", "-l", so[0]], text=True)
m = re.search(r"LC_BUILD_VERSION.*?minos (\d+)\.", lc, re.S)
minos = int(m.group(1)) if m else 999
print(f"  core.so minos = {minos}")
if minos > 14:
    print(f"  >>> core.so minos {minos} > 14 : NE CHARGERA PAS sous macOS {minos}-1."); sys.exit(1)
ml = f"{sp}/mlx/lib/mlx.metallib"
s = subprocess.run(["strings", ml], capture_output=True, text=True).stdout
tgt = re.findall(r"apple-macosx(\d+)\.", s)
worst = max((int(x) for x in tgt), default=14)
print(f"  metallib cible macosx max = {worst}")
if worst > 14:
    print(f"  >>> metallib cible macosx {worst} > 14 : GPU KO sous macOS {worst}-1."); sys.exit(1)
print("  OK MLX cible macOS 14 (core + metallib) — GPU sur tout Apple Silicon Sonoma+.")
PYEOF

echo "==> 4/6  Signature PROPRE (hors iCloud)"
# v28 — IDENTITÉ STABLE si disponible : avec « Vlocal Dev Signing » (cf.
# build/make_signing_identity.sh + commande de confiance admin), l'exigence
# désignée du bundle ne change plus d'un build à l'autre -> les permissions
# Accessibilité/Micro SURVIVENT aux mises à jour. Sinon : ad-hoc (permissions
# à re-accorder après chaque build).
SIGN_ID="-"
KC="$HOME/Library/Keychains/vlocal-sign.keychain-db"
if security find-identity -v -p codesigning "$KC" 2>/dev/null | grep -q "Vlocal Dev Signing"; then
  security unlock-keychain -p "vlocal-local-signing" "$KC" 2>/dev/null || true
  SIGN_ID="Vlocal Dev Signing"
  echo "  identité stable : $SIGN_ID (permissions conservées entre builds)"
else
  # GARDE-FOU (v28.1) : ne JAMAIS retomber en ad-hoc SILENCIEUSEMENT. L'ad-hoc
  # = nouvelle empreinte à chaque build = macOS empile des autorisations TCC
  # « fantômes » (vu 16 d'un coup) et l'Accessibilité casse sans réparation
  # possible via l'UI (seul `tccutil reset Accessibility com.vlocal.app` purge).
  # On AVERTIT en gros et on laisse 5 s pour annuler (Ctrl-C) plutôt que de
  # produire un build qui re-déclenchera tout le cauchemar des permissions.
  echo ""
  echo "  ┌───────────────────────────────────────────────────────────────┐"
  echo "  │ ⚠️  IDENTITÉ STABLE ABSENTE → SIGNATURE AD-HOC                  │"
  echo "  │ Ce build perdra les permissions Accessibilité/Micro et risque  │"
  echo "  │ de ré-empiler des autorisations TCC fantômes.                  │"
  echo "  │ POUR ÉVITER ÇA :                                               │"
  echo "  │   ./build/make_signing_identity.sh                             │"
  echo "  │   sudo security add-trusted-cert -d -r trustRoot -p codeSign \\ │"
  echo "  │     -k /Library/Keychains/System.keychain ~/.vlocal-sign-cert.pem │"
  echo "  │ puis relance ./build_app.sh.                                   │"
  echo "  └───────────────────────────────────────────────────────────────┘"
  echo "  (Ctrl-C pour annuler — reprise dans 5 s en ad-hoc…)"
  sleep 5
fi
# RACINE DU PROBLÈME (macOS 26+) : dist/ est sous ~/Desktop, qui peut être
# synchronisé iCloud (Bureau & Documents). macOS y colle alors com.apple.FinderInfo
# et com.apple.fileprovider.fpfs sur le bundle, et `codesign --deep --strict` les
# REFUSE : « resource fork, Finder information, or similar detritus not allowed ».
# (com.apple.provenance, lui, est IMMUABLE — xattr -cr ne l'enlève pas — mais il
# est TOLÉRÉ par codesign : ce n'est PAS lui qui bloque.)
# => On signe une copie dans un dossier TEMPORAIRE LOCAL (mktemp = /var/folders,
# hors iCloud) où FinderInfo/fileprovider ne sont jamais posés, et on fabrique le
# DMG depuis CETTE copie propre. Signer reste la toute dernière opération.
# v1.0.12 — Helper natif de capture audio système (mode VISIO de la réunion).
# Compilé DANS le bundle AVANT la signature : le codesign --deep ci-dessous le
# couvre (même identité que l'app). Échec/absent -> visio indisponible, présentiel
# strictement intact (dégradation gracieuse, aucune régression).
if command -v swiftc >/dev/null 2>&1 && [ -f syscapture.swift ]; then
  mkdir -p "$APP/Contents/Helpers"
  if swiftc -O syscapture.swift -o "$APP/Contents/Helpers/syscapture" \
       -framework Foundation -framework AVFoundation -framework ScreenCaptureKit \
       -framework CoreMedia; then
    echo "  ✅ helper visio syscapture embarqué (Contents/Helpers/)"
  else
    echo "  ⚠️ swiftc a échoué -> visio non embarquée (présentiel intact)"
    rm -f "$APP/Contents/Helpers/syscapture"
  fi
else
  echo "  ⚠️ swiftc/syscapture.swift absent -> visio non embarquée (présentiel intact)"
fi

SIGNDIR=$(mktemp -d)
SIGNAPP="$SIGNDIR/Vlocal.app"
ditto --norsrc --noextattr --noacl "$APP" "$SIGNAPP"
xattr -cr "$SIGNAPP" 2>/dev/null || true
codesign --force --deep --sign "$SIGN_ID" "$SIGNAPP"
codesign --verify --deep --strict "$SIGNAPP" && echo "  signature valide (propre, strict)" \
  || echo "  ATTENTION : signature non vérifiée"
# Verdict explicite « permissions survivront ou non » : l'exigence désignée
# ancrée sur le certificat (leaf = …) est STABLE d'un build à l'autre ; ancrée
# sur un cdhash ad-hoc, elle change à chaque fois (-> re-grant + fantômes TCC).
if codesign -dr- "$SIGNAPP" 2>&1 | grep -q "certificate leaf"; then
  echo "  ✅ exigence désignée STABLE (certificat) -> permissions Accessibilité/Micro CONSERVÉES aux prochains builds."
else
  echo "  ⚠️  exigence désignée ad-hoc (cdhash) -> permissions à RE-accorder + risque de fantômes TCC."
fi

echo "==> 5/6  Création du .dmg (depuis la copie signée propre)"
# DMG fabriqué depuis la copie /tmp signée -> l'app à l'intérieur est figée et
# garde sa signature stricte, même si le .dmg final atterrit dans dist/ (iCloud) :
# une image disque est un fichier unique, iCloud ne touche pas son contenu.
STAGE=$(mktemp -d)
ditto "$SIGNAPP" "$STAGE/Vlocal.app"
ln -s /Applications "$STAGE/Applications"
hdiutil create -volname "Vlocal" -srcfolder "$STAGE" -ov -format UDZO "$DMG" >/dev/null
rm -rf "$STAGE"
# On replace la version SIGNÉE dans dist/ (référence / test). NB : si dist est sous
# iCloud, ce bundle peut RE-accumuler FinderInfo AU REPOS et re-invalider la sign
# STRICTE — sans aucune incidence sur le DMG. Pour un run local fiable : installer
# depuis le DMG (glisser vers /Applications), pas en lançant dist/Vlocal.app.
rm -rf "$APP"
ditto "$SIGNAPP" "$APP"
rm -rf "$SIGNDIR"

# v29.2 — l'intermédiaire onedir dist/Vlocal (~3,4 Go, redondant avec le .app)
# n'est plus nécessaire : on le retire pour ne pas le re-synchroniser sur iCloud.
rm -rf dist/Vlocal 2>/dev/null || true

SIZE=$(du -sh "$APP" | cut -f1)
DMGSIZE=$(du -sh "$DMG" | cut -f1)
echo ""
echo "============================================================"
echo " TERMINÉ"
echo "   App  : $APP  ($SIZE)"
echo "   DMG  : $DMG  ($DMGSIZE)   <- à envoyer / déposer sur le Drive"
echo "============================================================"
echo " Rappel : au 1er lancement, clic DROIT sur Vlocal -> Ouvrir."
