#!/usr/bin/env python3
"""v15 — Tests du processor déterministe : apply_glossary + format_by_rules.
(Anciennement test du nettoyeur d'artefacts SLM, supprimé avec le SLM.)"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import processor


def t(case, got, want):
    ok = got == want
    print(f"  [{'OK' if ok else 'RATÉ'}] {case}")
    if not ok:
        print(f"    got  : {got!r}")
        print(f"    want : {want!r}")
    return ok


def main():
    n = ok = 0
    print("=== apply_glossary ===")
    g = [("Néolife", "Neolife"), ("Captive", "Captiv"), ("à Pifi", "Apify")]
    for case, got, want in [
        ("insensible casse", processor.apply_glossary("On voit Néolife.", g), "On voit Neolife."),
        ("casse mélangée", processor.apply_glossary("NÉOLIFE et néolife", g), "Neolife et Neolife"),
        ("frontière de mot", processor.apply_glossary("Captivement et Captive", g),
         "Captivement et Captiv"),
        ("multi-mots", processor.apply_glossary("on utilise à Pifi pour scraper", g),
         "on utilise Apify pour scraper"),
        ("glossaire vide", processor.apply_glossary("texte", []), "texte"),
        ("glossaire None", processor.apply_glossary("texte", None), "texte"),
    ]:
        n += 1; ok += t(case, got, want)

    print("=== format_by_rules ===")
    for case, got, want in [
        ("tics + capitalisation",
         processor.format_by_rules("alors euh on doit voir avec Pierre du coup"),
         "On doit voir avec Pierre."),
        ("espaces ponctuation",
         processor.format_by_rules("bonjour ,comment ça va ?"),
         "Bonjour, comment ça va?"),
    ]:
        n += 1; ok += t(case, got, want)

    # paragraphage par pauses
    segs = [
        {"text": "bonjour on commence", "start": 0.0, "end": 2.0},
        {"text": "premier point le budget", "start": 5.0, "end": 7.0},  # pause 3s
    ]
    out = processor.format_by_rules("", segments=segs)
    n += 1; ok += t("paragraphe par pause (>2s)", "\n\n" in out, True)

    print(f"\n{ok}/{n} OK")
    return 0 if ok == n else 1


if __name__ == "__main__":
    raise SystemExit(main())
