#!/usr/bin/env bash
# =============================================================================
# Vlocal — Notarisation Apple (à lancer par le fondateur, avec son compte Dev).
# Re-signe l'app avec ton certificat "Developer ID Application" + hardened runtime
# + les entitlements MLX, recrée le DMG, l'envoie à Apple, puis l'agrafe.
# Après ça : plus AUCUN dialogue Gatekeeper chez tes clients.
#
# v2 — CORRECTIF iCloud : on signe une COPIE PROPRE hors iCloud (sinon
# "resource fork, Finder information, or similar detritus not allowed").
#
# USAGE :
#   export DEV_ID="Developer ID Application: TON NOM (TEAMID)"
#   export APPLE_ID="ton-email@icloud.com"
#   export TEAM_ID="XXXXXXXXXX"
#   export APP_PW="xxxx-xxxx-xxxx-xxxx"
#   bash notarize.sh
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")"

APP="dist/Vlocal.app"
VER="$(cat VERSION 2>/dev/null || echo 3.2.8)"
# v1.0.13 — DMG_OUT = livrable final (dans dist/, sous iCloud). MAIS on NOTARISE
# depuis /tmp (DMG, défini plus bas), HORS iCloud : sinon iCloud téléverse le DMG
# de ~281 Mo EN MÊME TEMPS que notarytool -> liaison montante saturée (bufferbloat)
# -> upload Apple coupé (NWError 60 / deadlineExceeded). Copie finale vers dist/.
DMG_OUT="dist/Vlocal-${VER}.dmg"
ENT="entitlements.plist"

: "${DEV_ID:?Exporte DEV_ID=\"Developer ID Application: TON NOM (TEAMID)\"}"
# v3.2.8 — NOTARISATION SANS SECRET : profil keychain (créé une fois via
#   xcrun notarytool store-credentials vlocal-notary --apple-id … --team-id … --password …
# Plus besoin d'APPLE_ID/APP_PW dans l'environnement. Surchargeable via NOTARY_PROFILE.
NOTARY_PROFILE="${NOTARY_PROFILE:-vlocal-notary}"

[ -d "$APP" ] || { echo "❌ $APP introuvable — lance d'abord le build."; exit 1; }
[ -f "$ENT" ] || { echo "❌ $ENT introuvable."; exit 1; }

# COPIE PROPRE hors iCloud. dist/ est sous ~/Desktop (iCloud) qui pose
# com.apple.FinderInfo / fileprovider sur le bundle -> codesign --strict REFUSE
# ("detritus not allowed"). On signe une copie dans /var/folders (mktemp, hors
# iCloud) où ces xattrs n'existent jamais, et on fabrique le DMG depuis elle.
echo "▸ 0/5 — Copie propre (hors iCloud)…"
SIGNDIR=$(mktemp -d)
SIGNAPP="$SIGNDIR/Vlocal.app"
DMGDIR=$(mktemp -d); DMG="$DMGDIR/Vlocal-${VER}.dmg"   # DMG de travail HORS iCloud
ditto --norsrc --noextattr --noacl "$APP" "$SIGNAPP"
xattr -cr "$SIGNAPP" 2>/dev/null || true

# v1.0.13 — codesign ROBUSTE contre les timeouts transitoires du serveur de
# timestamp d'Apple (timestamp.apple.com). Sans retry+vérif, un binaire peut
# rester sans Developer ID + sans timestamp sécurisé -> Apple REJETTE tout le
# bundle (statut Invalid, cf. incident 24/06). On retry, on VÉRIFIE le timestamp,
# on échoue bruyamment si un binaire résiste (plutôt que soumettre à moitié signé).
_cs() {  # "$@" = args codesign ; la DERNIÈRE valeur = la cible à vérifier
  local target="${@: -1}" i info
  for i in 1 2 3 4 5; do
    if codesign "$@" 2>/tmp/vlocal_cs_err; then
      # Vérif du timestamp SANS pipe : "codesign -dvv | grep -q" sous pipefail
      # enverrait SIGPIPE à codesign -dvv (exit 141) -> pipefail jugerait la
      # vérif en échec À TORT (même quand le timestamp est bien là). On capture.
      info=$(codesign -dvv "$target" 2>&1 || true)
      case "$info" in *"Timestamp="*) return 0;; esac
    fi
    [ -s /tmp/vlocal_cs_err ] && sed 's/^/      /' /tmp/vlocal_cs_err >&2 || true
    echo "    ↻ retry $i — $(basename "$target") (timestamp Apple lent)…" >&2
    sleep $((i*3))
  done
  echo "  >>> ÉCHEC signature persistante : $target" >&2
  return 1
}

echo "▸ 1/5 — Signature des binaires natifs imbriqués (dylibs/.so : MLX, PyInstaller)…"
# Inside-out : signer le code natif imbriqué EN PROFONDEUR d'abord (ordre requis
# par Apple ; --deep est déconseillé pour la notarisation).
_NSIGN=0; _NFAIL=0
while IFS= read -r -d '' f; do
  if _cs --force --options runtime --timestamp --sign "$DEV_ID" "$f"; then
    _NSIGN=$((_NSIGN+1))
  else
    _NFAIL=$((_NFAIL+1))
  fi
done < <(find "$SIGNAPP" -type f \( -name "*.dylib" -o -name "*.so" \) -print0)
if [ "$_NFAIL" != "0" ]; then
  echo "  >>> $_NFAIL binaire(s) natif(s) non signables (Developer ID + timestamp)."
  echo "  >>> Serveur timestamp Apple instable -> RIEN n'a été soumis. RELANCE plus tard."
  exit 1
fi
echo "  ✅ $_NSIGN .dylib/.so signés Developer ID + timestamp sécurisé"

echo "▸ 1bis/5 — Signature du helper natif syscapture (mode visio, si présent)…"
if [ -f "$SIGNAPP/Contents/Helpers/syscapture" ]; then
  _cs --force --options runtime --timestamp --sign "$DEV_ID" "$SIGNAPP/Contents/Helpers/syscapture" \
    || { echo "  >>> helper syscapture non signable (timestamp Apple) — STOP."; exit 1; }
  echo "  helper syscapture signé (hardened runtime)"
fi

echo "▸ 2/5 — Signature du bundle (.app) avec hardened runtime + entitlements MLX…"
_cs --force --options runtime --timestamp --entitlements "$ENT" --sign "$DEV_ID" "$SIGNAPP" \
  || { echo "  >>> bundle non signable (timestamp Apple) — STOP."; exit 1; }
codesign --verify --deep --strict --verbose=2 "$SIGNAPP"
# v3.2.8 — GARDE-FOU MICRO : refuse de notariser une app SANS l'entitlement micro
# (sinon dictée muette « aucun son » sous hardened runtime chez TOUS les clients).
if ! codesign -d --entitlements - "$SIGNAPP" 2>&1 | grep -q "audio-input"; then
  echo "  >>> ENTITLEMENT MICRO ABSENT — notarisation STOPPÉE."
  echo "  >>> Ajoute com.apple.security.device.audio-input à entitlements.plist."
  exit 1
fi
echo "  ✅ entitlement micro présent (dictée OK sous hardened runtime)"

# Fabrique un DMG depuis SIGNAPP (fonction réutilisée pour les 2 packagings).
_make_dmg() {
  rm -f "$DMG"
  local STAGE; STAGE=$(mktemp -d)
  ditto "$SIGNAPP" "$STAGE/Vlocal.app"
  ln -s /Applications "$STAGE/Applications"
  hdiutil create -volname "Vlocal" -srcfolder "$STAGE" -ov -format UDZO "$DMG" >/dev/null
  rm -rf "$STAGE"
  _cs --force --timestamp --sign "$DEV_ID" "$DMG" \
    || { echo "  >>> DMG non signable (timestamp Apple) — STOP."; exit 1; }
}

# v1.0.13 — soumission ROBUSTE : l'upload du DMG (~280 Mo) vers le notary service
# d'Apple peut timeout en plein milieu (NWError 60 - Operation timed out) quand
# leur infra est dégradée (constaté 24/06 : 13 parts envoyées puis coupure).
# notarytool abandonne -> sans retry, set -e couperait tout le pipeline APRÈS la
# signature (gâchée pour rien). On retry l'upload complet ; STOP bruyant après N.
_submit() {  # $1 = fichier à soumettre (DMG)
  local i
  for i in 1 2 3 4 5 6 7 8 9 10; do
    if xcrun notarytool submit "$1" --keychain-profile "$NOTARY_PROFILE" --wait; then
      return 0
    fi
    echo "    ↻ soumission Apple #$i échouée (réseau/upload) — retry dans 45s…" >&2
    sleep 45
  done
  echo "  >>> Soumission Apple impossible après 4 tentatives (infra notary instable). RELANCE plus tard." >&2
  return 1
}

echo "▸ 3/6 — Création du DMG signé (pour notariser l'app + le conteneur)…"
_make_dmg

echo "▸ 4/6 — 1re notarisation : enregistre l'app + le DMG chez Apple…"
_submit "$DMG" || exit 1

# v1.0.5 — CRITIQUE (cf. memory vlocal-staple-gap) : agrafer l'APP D'ABORD, puis
# RECONSTRUIRE le DMG depuis l'app agrafée. Sinon l'app extraite du DMG (install
# manuelle OU swap de l'auto-updater) n'a PAS son propre ticket -> 1er lancement
# HORS-LIGNE bloqué par Gatekeeper. Le DMG reconstruit a un nouveau hash -> il faut
# le RE-soumettre pour pouvoir l'agrafer (sinon stapler « Record not found »).
echo "▸ 5/6 — Agrafage de l'APP, puis reconstruction du DMG depuis l'app agrafée…"
xcrun stapler staple "$SIGNAPP"
xcrun stapler validate "$SIGNAPP"
_make_dmg

echo "▸ 6/6 — 2e notarisation du DMG reconstruit + agrafage des DEUX couches…"
_submit "$DMG" || exit 1
xcrun stapler staple "$DMG"
# Garde-fou : vérifier que l'app DANS le DMG et le DMG lui-même sont agrafés.
_MNT=$(hdiutil attach -nobrowse -readonly "$DMG" | grep -o '/Volumes/.*' | head -1)
if ! xcrun stapler validate "$_MNT/Vlocal.app" >/dev/null 2>&1; then
  echo "  >>> APP NON AGRAFÉE dans le DMG — notarisation STOPPÉE."; hdiutil detach "$_MNT" >/dev/null 2>&1; exit 1
fi
hdiutil detach "$_MNT" >/dev/null 2>&1
xcrun stapler validate "$DMG" >/dev/null 2>&1 || { echo "  >>> DMG NON AGRAFÉ — STOP."; exit 1; }
echo "  ✅ app agrafée + DMG agrafé (1er lancement hors-ligne OK)"
# v1.0.13 — copie le DMG agrafé final dans dist/ (le travail s'est fait hors iCloud).
mkdir -p "$(dirname "$DMG_OUT")"; cp -f "$DMG" "$DMG_OUT"
xcrun stapler validate "$DMG_OUT" >/dev/null 2>&1 || { echo "  >>> copie dist/ non agrafée — STOP."; exit 1; }
# Remet la copie signée + agrafée dans dist/ (référence).
rm -rf "$APP"; ditto "$SIGNAPP" "$APP"
rm -rf "$SIGNDIR" "$DMGDIR"

echo ""
echo "✅ NOTARISÉ + AGRAFÉ : $DMG_OUT"
echo "   Tes clients pourront l'ouvrir sans aucun avertissement Gatekeeper."
echo "   → dis-moi « notarisé » : je réuploade ce DMG sur R2 et je republie."
