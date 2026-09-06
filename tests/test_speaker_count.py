"""Nombre de voix automatique (v1.1.0) : écart spectral avec repli silhouette."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import diarizer  # noqa: E402


def clusters(k, sizes, noise=0.045, seed=0):
    rng = np.random.default_rng(seed)
    C = rng.standard_normal((k, 192))
    C /= np.linalg.norm(C, axis=1, keepdims=True)
    parts = [C[i] + rng.standard_normal((sizes[i], 192)) * noise for i in range(k)]
    E = np.concatenate(parts).astype(np.float32)
    truth = np.concatenate([np.full(sizes[i], i) for i in range(k)])
    return E, truth


class SpeakerCountTests(unittest.TestCase):
    def test_finds_the_right_count_on_separable_voices(self):
        for k, sizes in ((2, (60, 100)), (3, (60, 100, 140)), (4, (60, 100, 140, 180))):
            E, _ = clusters(k, sizes)
            K, lab, sil = diarizer.estimate_speakers(E, 8, speech_s=3600)
            self.assertEqual(K, k, f"{k} grappes attendues")
            self.assertEqual(len(lab), len(E))

    def test_single_voice_is_one(self):
        E, _ = clusters(1, (120,))
        K, lab, _ = diarizer.estimate_speakers(E, 8, speech_s=600)
        self.assertEqual(K, 1)
        self.assertTrue((lab == 0).all())

    def test_unbalanced_four_voices_not_collapsed(self):
        # cas réel : deux voix dominantes, deux voix rares (la silhouette
        # pouvait rendre 2)
        E, _ = clusters(4, (400, 300, 40, 30), noise=0.06, seed=3)
        K, _, _ = diarizer.estimate_speakers(E, 6, speech_s=3600)
        self.assertEqual(K, 4)

    def test_speech_duration_cap_still_applies(self):
        E, _ = clusters(4, (60, 60, 60, 60))
        K, _, _ = diarizer.estimate_speakers(E, 8, speech_s=40)   # 40 s -> 2 voix max
        self.assertLessEqual(K, 2)

    def test_too_few_regions_falls_back_gracefully(self):
        # sous 8 régions l'écart spectral s'abstient : repli silhouette, borné
        E, _ = clusters(2, (3, 3))
        K, lab, _ = diarizer.estimate_speakers(E, 8)
        self.assertTrue(1 <= K <= len(E) - 1)
        self.assertEqual(len(lab), 6)
        K2, _, _ = diarizer.estimate_speakers(E, 8, speech_s=20)   # 20 s -> 1 voix max
        self.assertEqual(K2, 1)

    def test_eigengap_unreliable_returns_none(self):
        self.assertIsNone(diarizer._eigengap_k(np.zeros((4, 192), dtype=np.float32), 6))


if __name__ == "__main__":
    unittest.main()
