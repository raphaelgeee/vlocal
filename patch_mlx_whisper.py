#!/usr/bin/env python3
"""Patch idempotent de mlx_whisper (vendored) — RAM dictée.

`import mlx_whisper` charge transcribe.py qui importe timing.py AU NIVEAU MODULE,
or timing.py importe numba + scipy (~113 Mo de RAM mesurés). Ces deux paquets ne
servent QU'aux word-timestamps (alignement DTW), utilisés en RÉUNION/IMPORT mais
JAMAIS en dictée (word_timestamps=False). On rend cet import PARESSEUX : il
n'est exécuté que dans le bloc `if word_timestamps:`. Donc la dictée n'embarque
plus jamais ces 113 Mo ; la réunion les charge à la première fenêtre (sans gravité).

Sûr et réversible. Idempotent (marqueur). Lancé par build_app.sh avant l'empaquetage
PyInstaller, et à la main après toute réinstallation du venv.
"""
import os
import mlx_whisper

PATH = os.path.join(os.path.dirname(mlx_whisper.__file__), "transcribe.py")
MARK = "# vlocal: import timing déféré (RAM dictée)"

src = open(PATH, encoding="utf-8").read()
if MARK in src:
    print(f"mlx_whisper déjà patché (timing paresseux) : {PATH}")
    raise SystemExit(0)

TOP = "from .timing import add_word_timestamps\n"
if TOP not in src:
    raise SystemExit("ECHEC : import top-level de timing introuvable — "
                     "mlx_whisper a changé, vérifier le patch.")
src = src.replace(TOP, f"{MARK}\n")

HOOK = ("                if word_timestamps:\n"
        "                    add_word_timestamps(\n")
REPL = ("                if word_timestamps:\n"
        "                    from .timing import add_word_timestamps  # paresseux\n"
        "                    add_word_timestamps(\n")
if HOOK not in src:
    raise SystemExit("ECHEC : bloc 'if word_timestamps:' introuvable.")
src = src.replace(HOOK, REPL, 1)

open(PATH, "w", encoding="utf-8").write(src)
print(f"mlx_whisper patché (import timing déféré -> -113 Mo en dictée) : {PATH}")
