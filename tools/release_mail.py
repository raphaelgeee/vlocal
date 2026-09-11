#!/usr/bin/env python3
"""Mail de nouvelle version : rendu du gabarit et liste des destinataires.

    ./venv/bin/python tools/release_mail.py mail/1.3.3.json
    ./venv/bin/python tools/release_mail.py mail/1.3.3.json --destinataires

Rendu : dist/mail-<version>.html, à ouvrir pour relecture. L'objet et le texte
d'aperçu sont imprimés. Rien n'est envoyé par ce script : l'envoi passe par la
boîte du mainteneur, après relecture du rendu ET de la liste (docs/RELEASING.md,
étape 7). Les adresses viennent de la console d'administration (installations
qui ont laissé une adresse), plus un fichier CSV facultatif d'anciens
utilisateurs. Le jeton d'administration est lu sur le Bureau, jamais affiché.
"""
import json
import os
import re
import sys
import urllib.request

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GABARIT = os.path.join(RACINE, "mail", "nouvelle-version.html")
ADMIN = "https://lvcqgfjyhjujqjgrckeg.supabase.co/functions/v1/admin"
JETON = os.path.expanduser("~/Desktop/VLOCAL_ADMIN_JETON.txt")


def rendre(spec):
    html = open(GABARIT, encoding="utf-8").read()
    champs = set(re.findall(r"\{\{(\w+)\}\}", html))
    manquants = sorted(c for c in champs if c not in spec)
    if manquants:
        raise SystemExit("champs manquants dans la fiche : " + ", ".join(manquants))
    for cle in champs:
        html = html.replace("{{" + cle + "}}", str(spec[cle]))
    reste = re.findall(r"\{\{\w+\}\}", html)
    assert not reste, reste
    return html


def destinataires(csv_supplementaire=None, exclure=("miria.ai",)):
    adresses = set()
    try:
        L = open(JETON, encoding="utf-8").read().splitlines()
        tok = L[L.index("JETON :") + 1].strip()
        req = urllib.request.Request(ADMIN, data=json.dumps({"token": tok, "action": "overview"}).encode(),
                                     headers={"content-type": "application/json", "origin": "https://www.vlocal.org"})
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.load(r)
        for x in d.get("installs", []):
            if x.get("email"):
                adresses.add(x["email"].strip().lower())
    except Exception as e:
        print("  (console injoignable : " + str(e) + ")", file=sys.stderr)
    if csv_supplementaire and os.path.exists(csv_supplementaire):
        import csv
        for row in csv.DictReader(open(csv_supplementaire, encoding="utf-8")):
            if row.get("email"):
                adresses.add(row["email"].strip().lower())
    return sorted(a for a in adresses if not any(a.endswith("@" + dom) for dom in exclure))


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    spec = json.load(open(sys.argv[1], encoding="utf-8"))
    html = rendre(spec)
    os.makedirs(os.path.join(RACINE, "dist"), exist_ok=True)
    out = os.path.join(RACINE, "dist", "mail-%s.html" % spec["version"])
    open(out, "w", encoding="utf-8").write(html)
    print("Objet   :", spec["objet"])
    print("Aperçu  :", spec["apercu"])
    print("Rendu   :", out, "(%d Ko)" % (len(html.encode("utf-8")) // 1024))
    if "--destinataires" in sys.argv:
        csv_sup = os.path.join(RACINE, "_private", "licenses-backup-2026-09-08.csv")
        for a in destinataires(csv_sup):
            print("  ", a)


if __name__ == "__main__":
    main()
