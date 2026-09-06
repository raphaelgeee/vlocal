#!/bin/bash
# ============================================================================
# Vlocal — RÉPARATION des permissions (filet de sécurité).
#
# À n'utiliser QUE si l'insertion au curseur ne marche plus alors que
# l'interrupteur Accessibilité de Vlocal a l'air activé : signe qu'un ancien
# build (ad-hoc) a empilé des autorisations TCC « fantômes » que l'interface
# Réglages ne nettoie pas. Cette commande les purge ; macOS te redemandera
# proprement au prochain lancement.
#
# AVEC LA SIGNATURE STABLE (build/make_signing_identity.sh + cert approuvé),
# tu ne devrais JAMAIS en avoir besoin : les permissions survivent aux builds.
#
# Double-clique ce fichier (ou : bash build/repair_permissions.command).
# Ne lance PAS Vlocal — tu dois le rouvrir toi-même ensuite.
# ============================================================================
echo "═══════════════════════════════════════════════════"
echo "  Purge des autorisations Accessibilité fantômes"
echo "═══════════════════════════════════════════════════"
echo ""
# Ferme toute instance en cours pour repartir propre.
pkill -9 -f "Vlocal.app/Contents/MacOS/Vlocal" 2>/dev/null
N=$(tccutil reset Accessibility com.vlocal.app 2>&1 | grep -c "Successfully")
echo "✅ ${N} autorisation(s) Accessibilité effacée(s) pour com.vlocal.app."
echo ""
echo "MAINTENANT, dans l'ordre :"
echo "  1) Rouvre Vlocal toi-même : Finder → Applications → Vlocal"
echo "  2) macOS affiche « Vlocal souhaite contrôler cet ordinateur »"
echo "     → clique « Ouvrir les Réglages Système »"
echo "  3) Active l'interrupteur Vlocal (entrée NEUVE, plus de fantôme)"
echo "  4) L'insertion remarche aussitôt (détecteur en direct, sans relancer)"
echo ""
echo "Tu peux fermer cette fenêtre."
