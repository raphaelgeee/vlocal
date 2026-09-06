#!/usr/bin/env bash
# install_local.sh — installe PROPREMENT la derniere Vlocal-<VERSION>.dmg de dist/
# dans /Applications, en REMPLACANT toute version precedente. Zero doublon, zero
# fantome Launchpad. Pour les tests locaux. NE LANCE PAS l'app (regle TCC : un
# auto-launch empoisonne l'autorisation Accessibilite). Lance Vlocal a la main apres.
set -uo pipefail
cd "$(dirname "$0")"
VER="$(cat VERSION 2>/dev/null || echo '?')"
DMG="dist/Vlocal-${VER}.dmg"
[ -f "$DMG" ] || { echo "❌ DMG introuvable : $DMG (build d'abord)"; exit 1; }

echo "▸ Installation propre de Vlocal ${VER}"

# 1. Quitter l'app si elle tourne
if pgrep -x Vlocal >/dev/null 2>&1; then
  osascript -e 'quit app "Vlocal"' 2>/dev/null || true
  sleep 2
  pkill -x Vlocal 2>/dev/null || true
  echo "  app quittee"
fi

# 2. Ancienne version -> Corbeille (reversible, jamais de rm -rf)
if [ -d /Applications/Vlocal.app ]; then
  mv /Applications/Vlocal.app "$HOME/.Trash/Vlocal-remplacee-$(date +%s).app" && echo "  ancienne -> Corbeille"
fi

# 3. Monter le DMG (notarise), copier, demonter
MNT="$(hdiutil attach "$DMG" -nobrowse -readonly 2>/dev/null | awk -F'\t' '/\/Volumes\//{print $NF}' | tail -1)"
if [ -d "$MNT/Vlocal.app" ]; then
  cp -R "$MNT/Vlocal.app" /Applications/ && echo "  installee dans /Applications"
else
  echo "  ❌ Vlocal.app absente du DMG monte ($MNT)"; hdiutil detach "$MNT" -quiet 2>/dev/null; exit 1
fi
hdiutil detach "$MNT" -quiet 2>/dev/null || diskutil unmount "$MNT" 2>/dev/null || true

# 3bis. ANTI-DOUBLON DEFINITIF (regle Raphael : TOUJOURS une seule icone Vlocal).
# L'artefact de build dist/Vlocal.app n'est plus necessaire apres install (le DMG
# est l'artefact). Le laisser -> LaunchServices l'enregistre -> 2e icone "Vlocal"
# fantome dans Launchpad/Spotlight. On le met en Corbeille (reversible) +
# .metadata_never_index sur dist/ (Spotlight) + re-enregistre la VRAIE app.
# NB : les clients n'ont jamais de dist/ -> jamais de doublon chez eux.
LSREG="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
[ -d dist/Vlocal.app ] && mv dist/Vlocal.app "$HOME/.Trash/Vlocal-build-$(date +%s).app" 2>/dev/null && echo "  artefact dist/Vlocal.app -> Corbeille (anti-doublon)"
touch dist/.metadata_never_index 2>/dev/null || true
"$LSREG" -f /Applications/Vlocal.app 2>/dev/null || true

# 4. Vider le cache Launchpad (anti-fantome)
defaults write com.apple.dock ResetLaunchPad -bool true 2>/dev/null || true
killall Dock 2>/dev/null || true

# 5. Verifier
echo "▸ Verif :"
printf "  version : "; /usr/libexec/PlistBuddy -c "Print :CFBundleShortVersionString" /Applications/Vlocal.app/Contents/Info.plist 2>/dev/null
spctl -a -vv /Applications/Vlocal.app 2>&1 | grep -iE "accepted|source=" | head -2
N="$(ls -d /Applications/*[Vv]local*.app 2>/dev/null | wc -l | tr -d ' ')"
echo "  nombre de Vlocal.app installees : $N (doit etre 1)"
echo "✅ Termine. Lance Vlocal a la main (Spotlight) + autorise Accessibilite/Micro."
