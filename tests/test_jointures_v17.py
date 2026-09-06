#!/usr/bin/env python3
"""
Vlocal v17 — Tests des jointures de fenêtres (live_meeting.py).

Rejoue les 4 cas d'artefacts observés sur 2 réunions réelles (ElevenLabs). Les
WAV originaux n'étant pas disponibles, on simule chaque JOINTURE par des textes
de fenêtres mockés — c'est exactement la couche que v17 durcit (filtrage
métatexte + dédup de jointure + soin de troncature). Là où l'overlap entre en
jeu (Cas 1 et 4), la fenêtre N+1 est mockée TELLE QU'ELLE SERAIT ré-entendue
avec le chevauchement (mots précédents ré-transcrits).

Verdict par cas : VALIDE / À DURCIR. Honnêteté > optimisme.
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from live_meeting import (
    merge_window_texts, strip_metatext, _overlap_ngram, _norm_word,
)

METATEXT_MARKERS = [
    "transcription en français", "termes propres au contexte",
    "le mot de l'uk", "sous-titres", "sous-titrage", "merci d'avoir",
]


def _has_metatext(text):
    low = text.lower()
    return [m for m in METATEXT_MARKERS if m in low]


def _has_adjacent_ngram_dup(text, n=3):
    """True s'il existe un n-gram (>=n mots) répété IMMÉDIATEMENT (artefact de
    jointure). Ex. 'on a besoin de on a besoin de'."""
    w = [_norm_word(x) for x in text.split() if _norm_word(x)]
    for i in range(len(w) - 2 * n + 1):
        if w[i:i + n] == w[i + n:i + 2 * n]:
            return " ".join(w[i:i + n])
    return ""


def _keyword_coverage(text, keywords):
    low = text.lower()
    hit = sum(1 for k in keywords if k.lower() in low)
    return hit / len(keywords) if keywords else 1.0


# --------------------------------------------------------------------------- #
# Les 4 cas. `windows` = textes de fenêtres consécutives à fusionner.
CASES = [
    {
        "nom": "Cas 1 — Duplication/troncature à la frontière",
        "windows": [
            "ils naviguent constamment en",   # N : coupé mid-mot ('entre')
            # N+1 ré-entendue via overlap (ré-inclut les mots précédents) :
            "ils naviguent constamment entre les deux et qu'ils perdent le contexte",
        ],
        "keywords": ["naviguent", "constamment", "entre", "deux", "perdent", "contexte"],
        "cov_min": 0.80,
        "attendu_propre": True,
    },
    {
        "nom": "Cas 2 — Fragment tronqué + fenêtre 100% métatexte",
        "windows": [
            "je sors un PR demain matin avec le pro",   # 'prompt' tronqué en 'pro'
            "Transcription en français. Termes propres au contexte. Le mot de l'UK.",
        ],
        "keywords": ["sors", "PR", "demain", "matin"],   # la fin est PERDUE (honnête)
        "cov_min": 0.80,
        "attendu_propre": True,   # métatexte retiré, troncature marquée par …
    },
    {
        "nom": "Cas 3 — Mot mal entendu à travers une coupure (limite connue)",
        "windows": [
            "L'exploit.",                                  # 'l'export' mal entendu
            "Le support PDF avec personnalisation du header",
        ],
        "keywords": ["PDF", "personnalisation", "header"],
        "cov_min": 0.80,
        "attendu_propre": True,   # pas de métatexte/dup, MAIS mishearing non réparable
    },
    {
        "nom": "Cas 4 — Phrase tronquée à la jointure (récupérée via overlap)",
        "windows": [
            "On a besoin de 3",   # 'trancher' tronqué en '3'
            "On a besoin de trancher la techno avant de chiffrer le projet",
        ],
        "keywords": ["trancher", "techno", "chiffrer", "projet"],
        "cov_min": 0.80,
        "attendu_propre": True,
    },
]


def run():
    print("=" * 70)
    print("VLOCAL v17 — TESTS JOINTURES (live_meeting.py)")
    print("=" * 70)
    verdicts = {}
    for c in CASES:
        merged, removed = merge_window_texts(c["windows"])
        meta = _has_metatext(merged)
        dup = _has_adjacent_ngram_dup(merged, n=3)
        cov = _keyword_coverage(merged, c["keywords"])
        ok = (not meta) and (not dup) and (cov >= c["cov_min"])
        verdict = "VALIDE" if ok else "À DURCIR"
        verdicts[c["nom"]] = verdict
        print(f"\n--- {c['nom']} ---")
        print(f"  fenêtres  : {c['windows']}")
        print(f"  fusion    : {merged!r}")
        if removed:
            print(f"  métatexte retiré : {removed}")
        print(f"  métatexte résiduel : {meta or 'aucun'}")
        print(f"  dup n-gram (>=3)   : {dup or 'aucune'}")
        print(f"  couverture mots-clés : {cov*100:.0f}% (min {c['cov_min']*100:.0f}%)")
        print(f"  >>> {verdict}")

    # Unit tests ciblés des primitives
    print("\n" + "-" * 70)
    print("PRIMITIVES")
    u = []
    u.append(("strip_metatext retire l'écho prompt",
              strip_metatext("Transcription en français. Termes propres au contexte : Captiv.")[0].strip() == ""))
    u.append(("strip_metatext préserve le vrai texte",
              strip_metatext("le projet avance bien")[0] == "le projet avance bien"))
    u.append(("_overlap_ngram détecte un 4-gram",
              _overlap_ngram("on a besoin de".split(), "on a besoin de trancher".split()) == 4))
    u.append(("_overlap_ngram ignore <3 mots",
              _overlap_ngram("les deux".split(), "les deux sont".split()) == 0))
    u.append(("pas de dédup sur texte légitime",
              merge_window_texts(["le chat dort", "le chien court"])[0] == "le chat dort le chien court"))
    for n, r in u:
        print(f"  {'OK ' if r else 'KO '} {n}")

    npass = sum(1 for _, r in u if r)
    print("\n" + "=" * 70)
    print("VERDICTS PAR CAS :")
    for nom, v in verdicts.items():
        print(f"  [{v:8s}] {nom}")
    print(f"PRIMITIVES : {npass}/{len(u)} OK")
    print("=" * 70)

    all_ok = all(v == "VALIDE" for v in verdicts.values()) and npass == len(u)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(run())
