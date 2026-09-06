"""Live-tail de dictée : recherche de silence non bornée et incrémentale (v1.1.0)."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from livetail import DictationLiveTail  # noqa: E402

SR = 16000


class FakeEngine:
    """Moteur minimal : un buffer audio et un journal des plages demandées."""

    def __init__(self, audio):
        self.audio = np.asarray(audio, dtype=np.float32)
        self.requests = []

    def recorded_samples(self):
        return len(self.audio)

    def snapshot_audio(self, start, end):
        self.requests.append((start, end))
        return self.audio[start:end].copy()

    def transcribe(self, window):
        return f"{len(window) / SR:.0f}s"


def speech(seconds, seed=0):
    rng = np.random.default_rng(seed)
    return (rng.standard_normal(int(seconds * SR)) * 0.1).astype(np.float32)


def silence(seconds):
    return np.zeros(int(seconds * SR), dtype=np.float32)


class FindCutTests(unittest.TestCase):
    def tail(self, audio):
        eng = FakeEngine(audio)
        return DictationLiveTail(eng, autostart=False), eng

    def test_silence_beyond_old_16s_window_is_found(self):
        # Parole continue 0-50 s, pause 50-51 s, parole 51-60 s. L'ancienne
        # recherche [24 s, 40 s] ne trouvait rien et restait bloquee.
        audio = np.concatenate([speech(50), silence(1), speech(9, seed=1)])
        tail, eng = self.tail(audio)
        cut = tail._find_cut(len(audio) - SR, SR)
        self.assertIsNotNone(cut)
        # coupe au milieu du premier creux de 0,4 s : 50,0 s + 0,2 s
        self.assertAlmostEqual(cut / SR, 50.2, delta=0.15)

    def test_no_silence_returns_none_and_scan_is_incremental(self):
        audio = speech(90)
        tail, eng = self.tail(audio)
        self.assertIsNone(tail._find_cut(len(audio) - SR, SR))
        first = eng.requests[-1]
        self.assertEqual(first[0], int(DictationLiveTail.TARGET_S * SR))
        # 5 s de parole en plus : seul le nouvel audio (plus le recouvrement)
        # doit etre relu.
        eng.audio = np.concatenate([eng.audio, speech(5, seed=2)])
        self.assertIsNone(tail._find_cut(len(eng.audio) - SR, SR))
        second = eng.requests[-1]
        self.assertLessEqual(second[1] - second[0],
                             5 * SR + DictationLiveTail.NEED_STEPS * int(0.1 * SR) + SR)

    def test_silence_straddling_two_scans_is_found(self):
        # Le silence commence a 45,0 s mais seuls 0,15 s sont disponibles au
        # premier passage ; au second, le recouvrement doit le retrouver entier.
        head = np.concatenate([speech(45), silence(0.15)])
        tail, eng = self.tail(head)
        self.assertIsNone(tail._find_cut(len(head), SR))
        eng.audio = np.concatenate([head, silence(0.85), speech(5, seed=3)])
        cut = tail._find_cut(len(eng.audio) - SR, SR)
        self.assertIsNotNone(cut)
        self.assertAlmostEqual(cut / SR, 45.2, delta=0.15)

    def test_cut_never_before_target(self):
        audio = np.concatenate([speech(10), silence(1), speech(30, seed=4)])
        tail, eng = self.tail(audio)
        # le seul silence (10-11 s) est avant la cible de 24 s : pas de coupe
        self.assertIsNone(tail._find_cut(len(audio) - SR, SR))

    def test_stop_and_collect_without_thread(self):
        tail, _ = self.tail(speech(1))
        self.assertEqual(tail.stop_and_collect(), ([], 0))


if __name__ == "__main__":
    unittest.main()
