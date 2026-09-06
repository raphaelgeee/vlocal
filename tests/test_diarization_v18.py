#!/usr/bin/env python3
"""
Vlocal v18 — Tests diarisation (backend sherpa-onnx).

Logique PURE (fusion, renommage, correction, repli) testée sans aucun modèle.
Le test de pipeline RÉEL (3 voix) ne tourne que si sherpa-onnx + modèles sont
présents ; sinon il est marqué SKIP (pas d'échec).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import diarizer
from diarizer import (
    merge_transcript_with_speakers, find_speaker_at, apply_speaker_names,
    reassign_portion, available,
)

P = F = 0


def chk(name, cond):
    global P, F
    P += bool(cond)
    F += (not cond)
    print(("  OK  " if cond else "  KO  ") + name)


# --------------------------------------------------------------------------- #
def test_merge():
    print("\n=== 1. Fusion mots Whisper <-> locuteurs ===")
    speakers = [
        {"start": 0.0, "end": 3.0, "speaker": "SPEAKER_00"},
        {"start": 3.0, "end": 6.0, "speaker": "SPEAKER_01"},
    ]
    words = [
        {"word": "Bonjour", "start": 0.0, "end": 0.6},
        {"word": "à", "start": 0.6, "end": 0.8},
        {"word": "tous", "start": 0.8, "end": 1.4},
        {"word": "Salut", "start": 3.2, "end": 3.8},
        {"word": "ça", "start": 3.8, "end": 4.1},
        {"word": "va", "start": 4.1, "end": 4.5},
    ]
    blocks = merge_transcript_with_speakers(words, speakers)
    chk("2 blocs produits", len(blocks) == 2)
    chk("bloc 1 = SPEAKER_00 'Bonjour à tous'",
        blocks[0]["speaker"] == "SPEAKER_00" and blocks[0]["text"] == "Bonjour à tous")
    chk("bloc 2 = SPEAKER_01 'Salut ça va'",
        blocks[1]["speaker"] == "SPEAKER_01" and blocks[1]["text"] == "Salut ça va")
    chk("find_speaker_at(0.5) = SPEAKER_00", find_speaker_at(0.5, speakers) == "SPEAKER_00")
    chk("find_speaker_at(4.0) = SPEAKER_01", find_speaker_at(4.0, speakers) == "SPEAKER_01")
    chk("find_speaker_at(trou 8.0) -> plus proche (SPEAKER_01)",
        find_speaker_at(8.0, speakers) == "SPEAKER_01")
    chk("find_speaker_at sans segments -> UNKNOWN",
        find_speaker_at(1.0, []) == "SPEAKER_UNKNOWN")


def test_rename():
    print("\n=== 3. Renommage des locuteurs ===")
    blocks = [
        {"speaker": "SPEAKER_00", "start": 0.0, "end": 3.0, "text": "Bonjour à tous"},
        {"speaker": "SPEAKER_01", "start": 3.0, "end": 6.0, "text": "Salut ça va"},
    ]
    names = {"SPEAKER_00": "Marie", "SPEAKER_01": "Lucas"}
    labelled = apply_speaker_names(blocks, names)
    chk("SPEAKER_00 -> label 'Marie'", labelled[0]["label"] == "Marie")
    chk("SPEAKER_01 -> label 'Lucas'", labelled[1]["label"] == "Lucas")
    chk("identifiant speaker INCHANGÉ (stable)", labelled[0]["speaker"] == "SPEAKER_00")
    chk("locuteur non renommé -> label vide",
        apply_speaker_names(blocks, {})[0]["label"] == "")


def test_correction():
    print("\n=== 4. Correction manuelle (réassignation de portion) ===")
    # Un bloc SPEAKER_00 dont la fin appartient en fait à SPEAKER_01.
    blocks = [
        {"speaker": "SPEAKER_00", "start": 0.0, "end": 6.0,
         "text": "Bonjour à tous je peux poser une question"},
        {"speaker": "SPEAKER_01", "start": 6.0, "end": 8.0, "text": "oui vas-y"},
    ]
    # « je peux poser une question » (mots 3..8) -> SPEAKER_01
    out = reassign_portion(blocks, 0, 3, 8, "SPEAKER_01")
    chk("bloc 0 réduit à 'Bonjour à tous'",
        out[0]["speaker"] == "SPEAKER_00" and out[0]["text"] == "Bonjour à tous")
    chk("portion réassignée fusionnée avec SPEAKER_01 suivant",
        out[1]["speaker"] == "SPEAKER_01"
        and out[1]["text"] == "je peux poser une question oui vas-y")
    chk("plus que 2 blocs après fusion adjacente", len(out) == 2)
    # bornes invalides -> inchangé
    chk("réassignation hors bornes -> inchangé",
        reassign_portion(blocks, 0, 5, 2, "SPEAKER_01") == blocks)
    chk("index de bloc invalide -> inchangé",
        reassign_portion(blocks, 9, 0, 1, "SPEAKER_01") == blocks)


def test_fallback():
    print("\n=== 2/repli. Robustesse sans modèle ===")
    d = diarizer.Diarizer(seg_model="/inexistant.onnx", emb_model="/inexistant.onnx")
    chk("diarize sur modèle absent -> [] (repli, pas d'erreur)",
        d.diarize("/tmp/diar/meeting3.wav") == [])
    chk("available() est un booléen", isinstance(available(), bool))


def test_real_pipeline():
    print("\n=== 5. Pipeline RÉEL + RAM (si modèles présents) ===")
    audio = "/tmp/diar/meeting3.wav"
    if not available() or not os.path.exists(audio):
        print("  SKIP — sherpa-onnx ou modèles/audio absents (non bloquant).")
        return
    import psutil
    p = psutil.Process(os.getpid())
    r0 = p.memory_info().rss / 1e9
    d = diarizer.Diarizer()
    segs = d.diarize(audio, num_speakers=3)
    r1 = p.memory_info().rss / 1e9
    n = len({s["speaker"] for s in segs})
    print(f"  RAM diariseur chargé : {r1:.2f} Go (delta {r1-r0:.2f} Go)")
    chk("3 locuteurs détectés (nombre connu)", n == 3)
    # CAM++ (multilingue robuste) ~0,75 Go ; reste léger et, via le relais RAM
    # (Whisper déchargé pendant la diarisation), le pic global tient sous 2,5 Go.
    chk("RAM diariseur < 1,0 Go (léger)", (r1 - r0) < 1.0)
    d.unload()


def test_streaming():
    print("\n=== 6. WhoTalks v1.1 — flux EN LIGNE (feed_window/finalize_stream) ===")
    audio = "/tmp/diar/meeting3.wav"
    if not available() or not os.path.exists(audio):
        print("  SKIP — sherpa-onnx ou modèles/audio absents (non bloquant).")
        return
    import time
    import wave
    import numpy as np
    w = wave.open(audio, "rb")
    sr = w.getframerate()
    a = np.frombuffer(w.readframes(w.getnframes()),
                      dtype=np.int16).astype(np.float32) / 32768.0
    w.close()
    d = diarizer.Diarizer()
    d.reset_stream()
    # simule le live : fenêtres de 5 s alimentées « pendant l'enregistrement »
    win = int(5 * sr)
    for off in range(0, len(a), win):
        d.feed_window(a[off:off + win], sr, off / sr)
    chk("empreintes accumulées en ligne (> 0)", d.stream_count > 0)
    t = time.time()
    segs = d.finalize_stream(num_speakers=3)
    dt = time.time() - t
    n = len({s["speaker"] for s in segs})
    print(f"  {d.stream_count} empreintes -> {len(segs)} segments, {n} voix, "
          f"clustering {dt:.3f}s")
    chk("3 voix retrouvées sur flux (nombre connu)", n == 3)
    chk("latence de clustering à l'arrêt < 1 s (pas de ralentissement)", dt < 1.0)
    chk("segments produits", len(segs) > 0)
    # idempotence du reset
    d.reset_stream()
    chk("reset_stream vide le buffer", d.stream_count == 0)
    chk("finalize_stream sans données -> [] (pas d'erreur)",
        d.finalize_stream() == [])
    d.unload()


def test_voiceid():
    print("\n=== 7. WhoTalks v1.2 — banque de voix connues (reconnaissance) ===")
    import tempfile
    import voiceid
    d = tempfile.mkdtemp()
    # deux empreintes 4-d L2 (suffit pour la logique cosinus)
    import math
    def n(v):
        m = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / m for x in v]
    alice = n([0.9, 0.1, 0.05, 0.02])
    alice2 = n([0.88, 0.13, 0.06, 0.0])     # même voix, légère variation
    bob = n([0.05, 0.1, 0.9, 0.1])
    # enrôlement : refusé sous le seuil de secondes, accepté au-dessus
    chk("enrôlement refusé si trop court",
        voiceid.enroll_or_update(d, "Alice", alice, 5.0) is None)
    vid = voiceid.enroll_or_update(d, "Alice", alice, 40.0)
    chk("enrôlement Alice OK (assez de secondes)", bool(vid))
    voiceid.enroll_or_update(d, "Bob", bob, 50.0)
    chk("2 voix connues stockées", len(voiceid.load(d)) == 2)
    # reconnaissance : alice2 -> Alice (match confiant), inconnu -> None
    known = voiceid.load(d)
    v, sc = voiceid.match(alice2, known)
    chk("alice2 reconnue comme Alice", v and v["name"] == "Alice")
    vu, _ = voiceid.match(n([0.3, 0.3, 0.3, 0.4]), known)
    chk("voix ambiguë/inconnue -> non nommée (précision)", vu is None)
    # exclusion (une personne ne peut pas être deux locuteurs)
    ve, _ = voiceid.match(alice2, known, exclude_ids={vid})
    chk("Alice exclue -> pas re-attribuée", ve is None or ve["name"] != "Alice")
    # mise à jour pondérée du centroïde (ré-enrôlement)
    voiceid.enroll_or_update(d, "Alice", alice2, 60.0)
    chk("ré-enrôlement met à jour, ne duplique pas", len(voiceid.load(d)) == 2)
    chk("compteur de réunions incrémenté",
        next(x for x in voiceid.load(d) if x["name"] == "Alice")["meetings"] == 2)
    # rename / delete
    chk("rename voix", voiceid.rename(d, vid, "Alice Martin"))
    chk("public_list sans centroïde",
        all("centroid" not in x for x in voiceid.public_list(d)))
    chk("delete voix", voiceid.delete(d, vid))
    chk("delete effectif", len(voiceid.load(d)) == 1)


def run():
    print("=" * 66)
    print("VLOCAL v18 — TESTS DIARISATION (sherpa-onnx)")
    print("=" * 66)
    test_merge()
    test_rename()
    test_correction()
    test_fallback()
    test_real_pipeline()
    test_streaming()
    test_voiceid()
    print("\n" + "=" * 66)
    print(f"RÉSULTAT : {P} OK / {F} KO")
    print("=" * 66)
    return 0 if F == 0 else 1


if __name__ == "__main__":
    sys.exit(run())
