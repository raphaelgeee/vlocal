"""Lot micro : porte de silence par fenêtres (critère 5).

La porte jugeait le buffer ENTIER : une phrase courte dans une longue dictée,
un gain faible ou un casque Bluetooth en HFP passaient sous le seuil et la
dictée était jetée comme « silence ». Elle juge désormais l'énergie par
fenêtres de 30 ms.
"""
import os
import sys
import threading
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine  # noqa: E402

SR = engine.SAMPLE_RATE
_RNG = np.random.default_rng(7)


def _bruit(s, niveau=0.0005):
    """Bruit de fond d'une pièce calme (~ -66 dBFS)."""
    return (_RNG.standard_normal(int(s * SR)) * niveau).astype(np.float32)


def _voix(s, amp):
    """Signal vocal grossier : fondamentale + harmonique, modulée en syllabes."""
    t = np.arange(int(s * SR)) / SR
    env = 0.6 + 0.4 * np.sin(2 * np.pi * 4.0 * t) ** 2
    x = amp * env * (np.sin(2 * np.pi * 180 * t) + 0.5 * np.sin(2 * np.pi * 360 * t)) / 1.5
    return x.astype(np.float32)


def _ancienne_porte(audio):
    """Le prédicat d'avant, sur le buffer entier (pour prouver la régression)."""
    rms = float(np.sqrt(np.mean(audio * audio)))
    peak = float(np.max(np.abs(audio)))
    return peak < engine.PEAK_GATE or rms < engine.RMS_GATE


class TestTableDeVerite(unittest.TestCase):

    def test_table(self):
        une_phrase = np.concatenate([_bruit(15), _voix(1.0, 0.02), _bruit(15)])
        cas = [
            ("silence numérique", np.zeros(SR * 5, np.float32), True),
            ("bruit de fond 30 s", _bruit(30), True),
            ("clic isolé (10 ms) dans le silence",
             np.concatenate([_bruit(2), np.full(160, 0.5, np.float32), _bruit(2)]), True),
            ("100 ms de voix seulement", np.concatenate([_bruit(2), _voix(0.1, 0.05)]), True),
            ("1 s de voix dans 30 s de silence", une_phrase, False),
            ("un « oui » de 300 ms", np.concatenate([_bruit(1), _voix(0.3, 0.05), _bruit(1)]), False),
            ("voix forte continue", _voix(5, 0.3), False),
            ("Bluetooth HFP, gain faible (-40 dBFS crête)", _voix(5, 0.012), False),
            ("buffer vide", np.zeros(0, np.float32), True),
            ("buffer plus court qu'une fenêtre", _voix(0.01, 0.3), False),
        ]
        for nom, audio, attendu in cas:
            self.assertEqual(engine._is_silent(audio), attendu, nom)
        # La régression corrigée : l'ancienne porte jetait cette dictée.
        self.assertTrue(_ancienne_porte(une_phrase))

    def test_longue_dictee_rapide(self):
        import time
        audio = _bruit(600)                       # 10 min
        t0 = time.perf_counter()
        self.assertTrue(engine._is_silent(audio))
        self.assertLess(time.perf_counter() - t0, 0.5)


class TestCompatibiliteRaison(unittest.TestCase):
    """last_empty_reason() reste "gate" quand la porte refuse le buffer."""

    def _moteur(self):
        eng = engine.VlocalEngine.__new__(engine.VlocalEngine)
        eng._outcome_tls = threading.local()
        eng._outcome_lock = threading.Lock()
        eng._last_backend, eng._last_empty_reason, eng._last_producer = "", None, ""
        eng.last_device_name = "test"
        return eng

    def test_silence_donne_gate(self):
        eng = self._moteur()
        self.assertEqual(eng.stop_and_transcribe(audio=_bruit(10)), "")
        self.assertEqual(eng.last_empty_reason(), "gate")
        self.assertGreater(eng.last_rms, 0.0)      # diagnostics toujours publiés

    def test_phrase_courte_passe_la_porte(self):
        eng = self._moteur()
        appels = []
        eng.transcribe = lambda audio, vad=True, fast=False: appels.append(len(audio)) or "ok"
        audio = np.concatenate([_bruit(15), _voix(1.0, 0.02), _bruit(15)])
        self.assertEqual(eng.stop_and_transcribe(audio=audio), "ok")
        self.assertEqual(appels, [len(audio)])
        self.assertIsNone(eng.last_empty_reason())


if __name__ == "__main__":
    unittest.main()
