#!/usr/bin/env python3
"""Cohérence du dictionnaire i18n du dashboard (vlocal-interface.html).

Vérifie, avec node :
  - que l'objet I18N est du JavaScript valide ;
  - que les blocs fr et en ont exactement les mêmes clés ;
  - que toute clé utilisée dans le HTML (data-i18n, data-i18n-html, data-i18n-ph)
    ou dans le JS (t('...')) existe dans le dictionnaire ;
  - qu'aucune chaîne utilisateur ne contient de tiret cadratin.
Sortie non nulle en cas d'écart. Appelé par build_app.sh, utilisable à la main :
    ./venv/bin/python tests/check_i18n.py
"""
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = os.path.join(ROOT, "vlocal-interface.html")


def main() -> int:
    s = open(HTML, encoding="utf-8").read()
    i = s.index("  var I18N = {")
    j = s.index("\n  };", i) + 4
    obj = s[i:j]
    js = obj + (
        "\nconst fr=Object.keys(I18N.fr), en=Object.keys(I18N.en);"
        "const dash=[];for(const l of ['fr','en']){for(const k of Object.keys(I18N[l])){"
        "if(String(I18N[l][k]).includes('\\u2014')) dash.push(l+':'+k);}}"
        "console.log(JSON.stringify({fr:fr.length,en:en.length,"
        "onlyFr:fr.filter(k=>!(k in I18N.en)),onlyEn:en.filter(k=>!(k in I18N.fr)),dash}));"
    )
    r = subprocess.run(["node", "-e", js], capture_output=True, text=True)
    if r.returncode != 0:
        print("I18N : JavaScript invalide\n" + r.stderr.strip()[:800])
        return 1
    info = json.loads(r.stdout.strip())
    used = set(re.findall(r'data-i18n(?:-html|-ph)?="([^"]+)"', s))
    used |= set(re.findall(r"\bt\('([a-z0-9.\-_]+)'", s))
    keys = set(re.findall(r'"([a-z0-9.\-_]+)":"', obj))
    # clés construites dynamiquement (préfixes) : ignorées
    missing = sorted(k for k in used if k not in keys and not k.endswith(".")
                     and not k.startswith("label.hotkey."))
    problems = []
    if info["onlyFr"] or info["onlyEn"]:
        problems.append(f"clés non alignées fr/en : {info['onlyFr']} / {info['onlyEn']}")
    if missing:
        problems.append(f"clés utilisées absentes du dictionnaire : {missing}")
    if info["dash"]:
        problems.append(f"tiret cadratin dans une chaîne utilisateur : {info['dash']}")
    print(f"I18N : {info['fr']} clés fr, {info['en']} clés en")
    for p in problems:
        print("  ERREUR", p)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
