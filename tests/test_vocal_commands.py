#!/usr/bin/env python3
"""v1.0.14 — Tests de apply_vocal_commands (commandes vocales de mise en forme).

Déterministe, isolé. Vérifie :
  - sauts de ligne UNIQUEMENT après un marqueur de fin/clôture (jamais en
    milieu de phrase),
  - ponctuation vocale,
  - robustesse (vide, espaces, texte sans commande),
  - aucun contenu utilisateur supprimé.

Lancement : ./venv/bin/python tests/test_vocal_commands.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import processor

avc = processor.apply_vocal_commands


def t(case, got, want):
    ok = got == want
    print(f"  [{'OK' if ok else 'RATÉ'}] {case}")
    if not ok:
        print(f"    got  : {got!r}")
        print(f"    want : {want!r}")
    return ok


def main():
    n = ok = 0
    cases = []

    print("=== Sauts de ligne VALIDES (marqueur de fin présent) ===")
    cases += [
        ("après point", avc("bonjour. à la ligne comment allez-vous"),
         "bonjour.\ncomment allez-vous"),
        ("après voilà", avc("voilà à la ligne je continue"),
         "voilà\nje continue"),
        ("après ok", avc("ok à la ligne nouveau sujet"),
         "ok\nnouveau sujet"),
        ("après virgule", avc("premier point, à la ligne deuxième point"),
         "premier point,\ndeuxième point"),
        ("nouveau paragraphe", avc("c'est tout. nouveau paragraphe introduction"),
         "c'est tout.\n\nintroduction"),
        ("insensible à la casse", avc("VOILÀ À LA LIGNE suite"),
         "VOILÀ\nsuite"),
    ]

    print("=== Sauts par ponctuation APRÈS la commande (cas RÉEL dictée Whisper) ===")
    cases += [
        # Whisper place le point / ? / ! APRÈS « à la ligne » quand l'utilisateur
        # marque une pause -> c'est LE cas réel (bug remonté par un client le 26/06).
        ("à la ligne + point", avc("tu vas bien à la ligne. et toi"),
         "tu vas bien\net toi"),
        ("à la ligne + interrogation", avc("tout va bien à la ligne? je continue"),
         "tout va bien\nje continue"),
        ("nouveau paragraphe + point", avc("première partie nouveau paragraphe. seconde partie"),
         "première partie\n\nseconde partie"),
        ("dictée réelle complète (Raphaël, 26/06)",
         avc("Salut, j'espère que tu vas bien à la ligne. Est-ce que tout va bien à la ligne? Je m'appelle Raphaël."),
         "Salut, j'espère que tu vas bien\nEst-ce que tout va bien\nJe m'appelle Raphaël."),
        # ADVERSARIAL : la ponctuation doit être IMMÉDIATEMENT après la commande.
        # Ici un mot ('maintenant') s'intercale -> ce n'est PAS une commande.
        ("ponctuation après un mot intercalé -> phrase préservée",
         avc("j'ai terminé à la ligne maintenant."),
         "j'ai terminé à la ligne maintenant."),
    ]

    print("=== Sauts par VIRGULE de pause (cas réel Raphaël #2, 26/06) ===")
    cases += [
        # Whisper entoure souvent la commande de virgules pour marquer la pause.
        ("virgule sandwich (cas réel #2)",
         avc("Bonjour, ça va, à la ligne, j'espère que tu vas bien."),
         "Bonjour, ça va\nj'espère que tu vas bien."),
        ("virgule après seulement", avc("c'est noté à la ligne, on continue"),
         "c'est noté\non continue"),
        ("point-virgule après", avc("fin du point à la ligne; suite"),
         "fin du point\nsuite"),
        ("nouveau paragraphe entre virgules",
         avc("intro, nouveau paragraphe, développement"),
         "intro\n\ndéveloppement"),
        # ADVERSARIAL : une virgule PLUS LOIN (après le mot suivant) ne doit PAS
        # déclencher -> « à la ligne de bus, puis » reste une phrase.
        ("virgule éloignée -> contre-exemple préservé",
         avc("je vais à la ligne de bus, puis je rentre"),
         "je vais à la ligne de bus, puis je rentre"),
    ]

    print("=== Sauts de ligne INVALIDES (ni marqueur avant, ni ponctuation après) ===")
    cases += [
        ("ligne de bus", avc("je vais à la ligne de bus demain"),
         "je vais à la ligne de bus demain"),
        ("ligne des toits", avc("il habite à la ligne des toits"),
         "il habite à la ligne des toits"),
        ("terminé à la ligne", avc("j'ai terminé à la ligne maintenant"),
         "j'ai terminé à la ligne maintenant"),
        ("ligne budgétaire", avc("retour à la ligne budgétaire prévu"),
         "retour à la ligne budgétaire prévu"),
    ]

    print("=== Ponctuation vocale ===")
    cases += [
        ("virgules", avc("prix virgule qualité virgule rapidité"),
         "prix, qualité, rapidité"),
        ("interrogation", avc("vous avez des questions point d'interrogation"),
         "vous avez des questions ?"),
    ]

    print("=== Ponctuation AMBIGUË non déclenchée (jamais de perte) ===")
    cases += [
        # « deux points » volontairement NON converti (comme « point » seul) :
        # un nombre + nom légitime ne doit jamais être effacé.
        ("deux points littéral préservé", avc("on a deux points à voir"),
         "on a deux points à voir"),
        ("point seul préservé", avc("c'est un bon point pour nous"),
         "c'est un bon point pour nous"),
    ]

    print("=== Robustesse (jamais de perte de contenu) ===")
    cases += [
        ("vide", avc(""), ""),
        ("espaces seuls", avc("   "), ""),
        ("texte sans commande", avc("texte normal sans commande"),
         "texte normal sans commande"),
    ]

    for case, got, want in cases:
        n += 1
        if t(case, got, want):
            ok += 1

    print(f"\n{ok}/{n} OK")
    return 0 if ok == n else 1


if __name__ == "__main__":
    sys.exit(main())
