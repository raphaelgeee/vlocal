#!/usr/bin/env python3
"""Vlocal v15 — Auto-audit des 4 modes (100% déterministe, sans IA générative).

Couvre : DICTÉE (brut+glossaire), NOTE (règles+glossaire), RAPPEL (parser local),
RÉUNION (transcribe_detailed + règles + confidence). Audio réel via `say`.
"""
import datetime as dt
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine
import processor
import reminders

GLOSS = [("Captive", "Captiv"), ("Néolife", "Neolife")]
GLOSS_TERMS = [c for _, c in GLOSS]


def say(text):
    f = tempfile.mktemp(suffix=".wav")
    subprocess.run(["say", "-o", f, "--data-format=LEI16@16000", text], check=True)
    return f


def verdict(ok):
    return "VALIDE" if ok else "À DURCIR"


def main():
    print("=" * 68)
    print("Vlocal v15 — Auto-audit 4 modes (Whisper large-v3-turbo, zéro IA)")
    print("=" * 68)
    engine.set_correction_terms(GLOSS_TERMS)
    eng = engine.VlocalEngine(model_dir="models/whisper-large-v3-turbo-int8",
                              cpu_threads=4)
    results = []

    # ---- DICTÉE (court) ----
    f = say("On parle du projet Captiv avec Néolife aujourd'hui.")
    t0 = time.time(); txt = eng.transcribe(_load(f), mode="dictee"); dt0 = time.time()-t0
    txt = processor.apply_glossary(txt, GLOSS)
    ok = "Captiv" in txt and "Neolife" in txt
    print(f"\n[DICTÉE court] {dt0:.1f}s\n  {txt!r}\n  glossaire OK={ok} -> {verdict(ok)}")
    results.append(("DICTÉE court", verdict(ok)))
    os.unlink(f)

    # ---- NOTE (avec tics + glossaire) ----
    f = say("Alors euh il faut voir avec Captive pour le devis du coup.")
    txt = eng.transcribe(_load(f), mode="note")
    note = processor.format_by_rules(txt)
    note = processor.apply_glossary(note, GLOSS)
    ok = "Captiv" in note and "euh" not in note.lower() and note[:1].isupper()
    print(f"\n[NOTE] \n  brut={txt!r}\n  note={note!r}\n  tics retirés+glossaire+capit OK={ok} -> {verdict(ok)}")
    results.append(("NOTE", verdict(ok)))
    os.unlink(f)

    # ---- RAPPEL (échéance explicite, parser local — pas d'audio) ----
    now = dt.datetime(2026, 6, 6, 16, 0, 0).timestamp()
    r = reminders.parse_reminder("rappelle-moi demain à 14h de relancer Pierre", now)
    ok = r["datetime_iso"] == "2026-06-07 14:00" and "Pierre" in r["titre"]
    print(f"\n[RAPPEL explicite]\n  {r}\n  datetime+titre OK={ok} -> {verdict(ok)}")
    results.append(("RAPPEL explicite", verdict(ok)))
    r2 = reminders.parse_reminder("rappelle-moi dans 2 heures d'envoyer le mail", now)
    ok2 = r2["datetime_iso"] == "2026-06-06 18:00"
    print(f"[RAPPEL relatif]\n  {r2}\n  now+2h OK={ok2} -> {verdict(ok2)}")
    results.append(("RAPPEL relatif", verdict(ok2)))

    # ---- RÉUNION (détaillé + règles + confidence) ----
    f = say("Bonjour. Aujourd'hui on parle du projet Captiv. "
            "Le budget est de dix mille euros. On valide avec Néolife.")
    t0 = time.time(); d = eng.transcribe_detailed(f, mode="reunion"); dtr = time.time()-t0
    struct = processor.format_by_rules(d["text"], segments=d["segments"])
    struct = processor.apply_glossary(struct, GLOSS)
    ok = ("Captiv" in struct and "Neolife" in struct and d["avg_conf"] > 0.6
          and "amara" not in struct.lower())
    print(f"\n[RÉUNION] {dtr:.1f}s, avg_conf={d['avg_conf']}\n  {struct!r}\n"
          f"  glossaire+confiance+anti-hallu OK={ok} -> {verdict(ok)}")
    results.append(("RÉUNION", verdict(ok)))
    os.unlink(f)

    print("\n" + "=" * 68)
    print("SYNTHÈSE")
    for name, v in results:
        print(f"  {name:22} {v}")
    n_ok = sum(1 for _, v in results if v == "VALIDE")
    print(f"\n  {n_ok}/{len(results)} VALIDE")
    return 0


def _load(path):
    import wave
    import numpy as np
    with wave.open(path, "rb") as w:
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    return pcm.astype(np.float32) / 32768.0


if __name__ == "__main__":
    raise SystemExit(main())
