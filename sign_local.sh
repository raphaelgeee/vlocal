#!/usr/bin/env bash
# =============================================================================
# sign_local.sh — Signe l'app Developer ID + hardened runtime + entitlement micro
# (IDENTIQUE à notarize.sh, étapes 0→3) MAIS SANS l'upload Apple ni le staple.
#
# Pour un TEST LOCAL FIDÈLE quand l'infra notary d'Apple est KO (deadlineExceeded).
# - Developer ID Application (même cert que la version installée) -> la Designated
#   Requirement est identique -> TCC Accessibilité/Micro CONSERVÉS (pas de
#   ré-autorisation).
# - hardened runtime + entitlement com.apple.security.device.audio-input -> dictée
#   OK (fidèle au binaire distribué), pas le masquage dev.
# - PAS de --timestamp : zéro dépendance à Apple (le timestamp ne sert qu'à la
#   notarisation, refaite proprement par notarize.sh AVANT toute publication).
#
# Gatekeeper dira « Unnotarized Developer ID » -> Ouvrir quand même au 1er lancement.
#
# USAGE :  export DEV_ID="Developer ID Application: TON NOM (TEAMID)"; bash sign_local.sh
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")"

APP="dist/Vlocal.app"
VER="$(cat VERSION 2>/dev/null || echo 0.0.0)"
DMG_OUT="dist/Vlocal-${VER}.dmg"
ENT="entitlements.plist"

: "${DEV_ID:?Exporte DEV_ID=\"Developer ID Application: TON NOM (TEAMID)\"}"
[ -d "$APP" ] || { echo "❌ $APP introuvable — build d'abord."; exit 1; }
[ -f "$ENT" ] || { echo "❌ $ENT introuvable."; exit 1; }

echo "▸ 0/4 — Copie propre hors iCloud…"
SIGNDIR=$(mktemp -d); SIGNAPP="$SIGNDIR/Vlocal.app"
DMGDIR=$(mktemp -d); DMG="$DMGDIR/Vlocal-${VER}.dmg"
ditto --norsrc --noextattr --noacl "$APP" "$SIGNAPP"
xattr -cr "$SIGNAPP" 2>/dev/null || true

echo "▸ 1/4 — Signature des .dylib/.so (Developer ID + hardened runtime)…"
_NSIGN=0
while IFS= read -r -d '' f; do
  codesign --force --options runtime --sign "$DEV_ID" "$f"
  _NSIGN=$((_NSIGN+1))
done < <(find "$SIGNAPP" -type f \( -name "*.dylib" -o -name "*.so" \) -print0)
echo "  ✅ $_NSIGN .dylib/.so signés"

if [ -f "$SIGNAPP/Contents/Helpers/syscapture" ]; then
  codesign --force --options runtime --sign "$DEV_ID" "$SIGNAPP/Contents/Helpers/syscapture"
  echo "  helper syscapture signé (hardened)"
fi

echo "▸ 2/4 — Signature du bundle (hardened + entitlements micro)…"
codesign --force --options runtime --entitlements "$ENT" --sign "$DEV_ID" "$SIGNAPP"
codesign --verify --deep --strict --verbose=2 "$SIGNAPP"
# GARDE-FOU micro : refuse de produire une app SANS audio-input (sinon dictée muette
# sous hardened runtime).
if ! codesign -d --entitlements - "$SIGNAPP" 2>&1 | grep -q "audio-input"; then
  echo "  >>> ENTITLEMENT MICRO ABSENT — STOP (ajoute audio-input à $ENT)."; exit 1
fi
echo "  ✅ entitlement micro présent (dictée OK sous hardened runtime)"

echo "▸ 3/4 — Fabrication du DMG signé (depuis l'app signée propre)…"
rm -f "$DMG"
STAGE=$(mktemp -d)
ditto "$SIGNAPP" "$STAGE/Vlocal.app"
ln -s /Applications "$STAGE/Applications"
hdiutil create -volname "Vlocal" -srcfolder "$STAGE" -ov -format UDZO "$DMG" >/dev/null
rm -rf "$STAGE"
codesign --force --sign "$DEV_ID" "$DMG"

echo "▸ 4/4 — Dépôt dans dist/ (DMG + app de référence)…"
mkdir -p "$(dirname "$DMG_OUT")"; cp -f "$DMG" "$DMG_OUT"
rm -rf "$APP"; ditto "$SIGNAPP" "$APP"
rm -rf "$SIGNDIR" "$DMGDIR"

echo ""
echo "✅ SIGNÉ Developer ID + hardened + micro (NON notarisé) : $DMG_OUT"
echo "   spctl : « Unnotarized Developer ID » -> Ouvrir quand même au 1er lancement."
echo "   TCC Accessibilité/Micro CONSERVÉS (même DR que la version installée)."
echo "   ⚠️ RE-NOTARISER (notarize.sh) AVANT toute publication."
