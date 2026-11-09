"""Encodeur GPU accéléré (v1.6) : même sortie que l'encodeur de mlx_whisper,
mémo d'une fenêtre, branchement idempotent. Modèle minuscule à poids
aléatoires : aucun modèle réel chargé, rien d'écrit sur disque."""
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mlx_engine  # noqa: E402

try:
    import mlx.core as mx
    from mlx_whisper.whisper import AudioEncoder
except Exception:          # build sans MLX (Intel, CI Linux) : rien à tester
    mx = None


@unittest.skipIf(mx is None, "MLX indisponible")
class FastEncoderTests(unittest.TestCase):
    N_MELS, N_CTX, N_STATE, N_HEAD, N_LAYER = 8, 12, 32, 4, 3

    def setUp(self):
        mx.random.seed(7)
        self.enc = AudioEncoder(self.N_MELS, self.N_CTX, self.N_STATE, self.N_HEAD,
                                self.N_LAYER, dtype=mx.float32)
        self.x = mx.random.normal((1, 2 * self.N_CTX, self.N_MELS))
        self.ref = self.enc(self.x)              # encodeur d'origine
        mx.eval(self.ref)
        self.model = types.SimpleNamespace(encoder=self.enc)
        mlx_engine._forget_encoding()

    def tearDown(self):
        mlx_engine._forget_encoding()

    def test_meme_sortie_que_l_encodeur_d_origine(self):
        mlx_engine._accelerate(self.model)
        out = self.enc(self.x)
        self.assertEqual(out.shape, self.ref.shape)
        self.assertTrue(mx.allclose(out, self.ref, atol=1e-4, rtol=1e-4).item())

    def test_branchement_idempotent(self):
        mlx_engine._accelerate(self.model)
        cls = type(self.enc)
        mlx_engine._accelerate(self.model)
        self.assertIs(type(self.enc), cls)
        self.assertTrue(issubclass(cls, AudioEncoder))
        self.assertEqual(len(self.enc.parameters()), len(AudioEncoder.parameters(self.enc)))

    def test_memo_rend_la_fenetre_deja_encodee(self):
        mlx_engine._accelerate(self.model)
        first = self.enc(self.x)
        same_content = mx.array(self.x)          # autre objet, même contenu
        self.assertIs(self.enc(same_content), first)

    def test_memo_ignore_une_autre_fenetre(self):
        mlx_engine._accelerate(self.model)
        first = self.enc(self.x)
        other = self.enc(self.x + 1.0)
        self.assertIsNot(other, first)
        self.assertFalse(mx.allclose(other, first).item())

    def test_memo_vide_apres_transcription(self):
        mlx_engine._accelerate(self.model)
        first = self.enc(self.x)
        mlx_engine._forget_encoding()
        self.assertIsNone(mlx_engine._enc_memo)
        again = self.enc(self.x)
        self.assertIsNot(again, first)
        self.assertTrue(mx.allclose(again, first).item())


if __name__ == "__main__":
    unittest.main()
