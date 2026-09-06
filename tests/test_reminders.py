#!/usr/bin/env python3
"""Vlocal — 50 cas de détection de rappels (formulations variées).
Référence : vendredi 6 juin 2026, 16:00. Audit de robustesse du parser local.
"""
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import reminders

NOW = dt.datetime(2026, 6, 6, 16, 0, 0)  # SAMEDI 6 juin 2026, 16:00
NOW_TS = NOW.timestamp()

# (phrase, datetime_iso attendu OU None, fragment attendu dans le titre)
# datetime_iso None = on n'exige pas d'heure ; "*" = on exige une heure (peu importe)
CASES = [
    # --- v20 : « midi » ne doit JAMAIS matcher dans « après-midi » (bug \b
    # avec le tiret), le titre ne doit pas être amputé, et les fractions de
    # midi/minuit sont gérées. NOW = samedi 16:00 -> « cet après-midi » passé
    # roule au lendemain via le moment 14:00... non : jour même si futur, sinon
    # comportement existant (on n'asserte que l'HEURE 14:00 attendue le bon jour).
    ("rappelle-moi de sortir les poubelles demain après-midi", "2026-06-07 14:00", "poubelles"),
    ("rappelle-moi demain midi de signer le bail", "2026-06-07 12:00", "bail"),
    ("rappelle-moi demain à midi et demi de lancer la machine", "2026-06-07 12:30", "machine"),
    ("rappelle-moi à minuit et quart de couper le serveur", "2026-06-07 00:15", "serveur"),
    # --- déclencheurs variés ---
    ("rappelle-moi d'appeler Paul à 18h", "2026-06-06 18:00", "Paul"),
    ("rappelle moi d'appeler Paul à 18h", "2026-06-06 18:00", "Paul"),
    ("rappel : appeler Paul à 18h", "2026-06-06 18:00", "Paul"),
    ("n'oublie pas d'appeler Paul à 18h", "2026-06-06 18:00", "Paul"),
    ("souviens-toi d'appeler Paul à 18h", "2026-06-06 18:00", "Paul"),
    ("pense à appeler Paul à 18h", "2026-06-06 18:00", "Paul"),
    ("il faut que je rappelle Paul à 18h", "2026-06-06 18:00", "Paul"),
    ("faut que j'appelle Paul à 18h", "2026-06-06 18:00", "Paul"),
    ("note d'appeler Paul à 18h", "2026-06-06 18:00", "Paul"),
    ("rappelle-moi d'appeler Paul ce soir", "2026-06-06 19:00", "Paul"),
    # --- heures variées ---
    ("rappelle-moi à 18h30 de partir", "2026-06-06 18:30", "Partir"),
    ("rappelle-moi à 18 heures de partir", "2026-06-06 18:00", "Partir"),
    ("rappelle-moi à 18 heures 30 de partir", "2026-06-06 18:30", "Partir"),
    ("rappelle-moi à 8h du soir de partir", "2026-06-06 20:00", "Partir"),
    ("rappelle-moi à 8h du matin de partir", "2026-06-07 08:00", "Partir"),
    ("rappelle-moi à midi de partir", "2026-06-07 12:00", "Partir"),     # midi<16h -> demain midi
    ("rappelle-moi à minuit de partir", "*", "Partir"),
    ("rappelle-moi à 7h pile de partir", "2026-06-07 07:00", "Partir"),
    ("rappelle-moi vers 18h de partir", "2026-06-06 18:00", "Partir"),
    ("rappelle-moi à 19h de partir", "2026-06-06 19:00", "Partir"),
    ("rappelle-moi à 6h et demie de partir", "2026-06-07 06:30", "Partir"),
    ("rappelle-moi à 9h45 de partir", "2026-06-07 09:45", "Partir"),
    ("rappelle-moi à 14h15 de partir", "2026-06-07 14:15", "Partir"),   # 14h15 passé -> demain
    # --- relatif ---
    ("rappelle-moi dans 2 heures d'appeler Paul", "2026-06-06 18:00", "Paul"),
    ("rappelle-moi dans une heure d'appeler Paul", "2026-06-06 17:00", "Paul"),
    ("rappelle-moi dans 30 minutes d'appeler Paul", "2026-06-06 16:30", "Paul"),
    ("rappelle-moi dans 10 min d'appeler Paul", "2026-06-06 16:10", "Paul"),
    ("rappelle-moi dans un quart d'heure d'appeler Paul", "2026-06-06 16:15", "Paul"),
    ("rappelle-moi dans une demi-heure d'appeler Paul", "2026-06-06 16:30", "Paul"),
    ("rappelle-moi dans 3 jours d'appeler Paul", "2026-06-09 09:00", "Paul"),
    ("rappelle-moi d'ici 2 heures d'appeler Paul", "2026-06-06 18:00", "Paul"),
    # --- jours ---
    ("rappelle-moi demain d'appeler Paul", "2026-06-07 09:00", "Paul"),
    ("rappelle-moi demain à 14h d'appeler Paul", "2026-06-07 14:00", "Paul"),
    ("rappelle-moi demain matin d'appeler Paul", "2026-06-07 09:00", "Paul"),
    ("rappelle-moi après-demain d'appeler Paul", "2026-06-08 09:00", "Paul"),
    ("rappelle-moi lundi d'appeler Paul", "2026-06-08 09:00", "Paul"),
    ("rappelle-moi lundi prochain d'appeler Paul", "2026-06-08 09:00", "Paul"),
    ("rappelle-moi mercredi à 9h30 d'appeler Paul", "2026-06-10 09:30", "Paul"),
    ("rappelle-moi vendredi prochain d'appeler Paul", "2026-06-12 09:00", "Paul"),
    ("rappelle-moi ce week-end d'appeler Paul", "*", "Paul"),
    ("rappelle-moi samedi à 10h d'appeler Paul", "2026-06-13 10:00", "Paul"),
    # --- dates ---
    ("rappelle-moi le 15 d'appeler Paul", "*", "Paul"),
    ("rappelle-moi le 15 juin d'appeler Paul", "2026-06-15 09:00", "Paul"),
    ("rappelle-moi le 20 juin à 14h d'appeler Paul", "2026-06-20 14:00", "Paul"),
    # --- avant / un peu avant ---
    ("rappelle-moi d'envoyer le mail avant 18h", "2026-06-06 17:45", "Envoyer le mail"),
    ("rappelle-moi un peu avant 18h d'envoyer le mail", "2026-06-06 17:50", "Envoyer le mail"),
    # --- moments ---
    ("rappelle-moi cet après-midi d'appeler Paul", "*", "Paul"),
    ("rappelle-moi ce matin d'appeler Paul", "*", "Paul"),
    # --- sans heure ---
    ("pense à acheter du pain", None, "Acheter du pain"),
    ("rappelle-moi d'appeler le dentiste", None, "Appeler le dentiste"),
]


def run():
    n = len(CASES)
    ok = 0
    fails = []
    for phrase, want_iso, want_title in CASES:
        r = reminders.parse_reminder(phrase, NOW_TS)
        got_iso = r["datetime_iso"]
        title_ok = want_title.lower() in (r["titre"] or "").lower()
        if want_iso is None:
            iso_ok = got_iso is None
        elif want_iso == "*":
            iso_ok = got_iso is not None
        else:
            iso_ok = got_iso == want_iso
        if iso_ok and title_ok:
            ok += 1
        else:
            fails.append((phrase, want_iso, got_iso, want_title, r["titre"]))
    print(f"{ok}/{n} cas OK\n")
    if fails:
        print("ÉCHECS :")
        for phrase, wi, gi, wt, gt in fails:
            print(f"  IN  : {phrase}")
            print(f"     iso attendu={wi!r} obtenu={gi!r} | titre attendu~{wt!r} obtenu={gt!r}")
    return ok, n


if __name__ == "__main__":
    ok, n = run()
    raise SystemExit(0 if ok == n else 1)
