"""Travail GPU interruptible (v1.7) : une dictée mise en file interrompt le calcul
spéculatif en cours, jamais l'inverse ; points de contrôle de l'encodeur et du
décodeur sans effet sur la sortie. Inférence simulée ou modèle minuscule à
poids aléatoires : aucun modèle réel chargé, rien d'écrit sur disque."""
import os
import sys
import threading
import time
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mlx_engine  # noqa: E402

try:
    import mlx.core as mx
    from mlx_whisper.whisper import AudioEncoder, TextDecoder
except Exception:          # build sans MLX (Intel, CI Linux)
    mx = None


def _fake_impl(a, language, initial_prompt, word_timestamps,
               hallucination_silence_threshold=None, temperature=None, cancel=None):
    """Inférence simulée sur le worker : longue en fond (2 s, points de contrôle
    toutes les 5 ms), immédiate pour une dictée."""
    mlx_engine._job_cancel = cancel
    try:
        mlx_engine._check_cancel()
        if language == "fond":
            t_end = time.monotonic() + 2.0
            while time.monotonic() < t_end:
                mlx_engine._check_cancel()
                time.sleep(0.005)
        return {"text": language}
    finally:
        mlx_engine._job_cancel = None


class PreemptionTests(unittest.TestCase):
    def setUp(self):
        self._impl = mlx_engine._transcribe_impl
        mlx_engine._transcribe_impl = _fake_impl

    def tearDown(self):
        mlx_engine._transcribe_impl = self._impl
        mlx_engine.renew_worker()       # l'ancien worker s'arrête après sa file

    def _background(self, cancel, box):
        def run():
            t = time.monotonic()
            try:
                mlx_engine.transcribe([0.0], language="fond", cancel=cancel, _timeout=10)
                box["issue"] = "fini"
            except mlx_engine.Cancelled:
                box["issue"] = "interrompu"
            box["duree"] = time.monotonic() - t
        th = threading.Thread(target=run)
        th.start()
        return th

    def test_la_dictee_interrompt_le_speculatif_en_cours(self):
        box = {}
        th = self._background(mlx_engine.Cancel(), box)
        time.sleep(0.2)                                 # le calcul de fond tourne
        t = time.monotonic()
        r = mlx_engine.transcribe([0.0], language="dictee",
                                  _prio=mlx_engine.PRIO_DICTATION, _timeout=10)
        attente = time.monotonic() - t
        th.join(3)
        self.assertEqual(r["text"], "dictee")
        self.assertEqual(box["issue"], "interrompu")
        self.assertLess(attente, 0.3)                   # pas les 1,8 s restantes

    def test_travail_de_fond_sans_jeton_jamais_interrompu(self):
        # Réunion, import : priorité basse, mais pas interruptible.
        box = {}

        def run():
            mlx_engine.transcribe([0.0], language="fond", _timeout=10)
            box["fini"] = True
        th = threading.Thread(target=run)
        th.start()
        time.sleep(0.1)
        mlx_engine.transcribe([0.0], language="dictee",
                              _prio=mlx_engine.PRIO_DICTATION, _timeout=10)
        th.join(3)
        self.assertTrue(box.get("fini"))

    def test_jeton_leve_avant_le_tour_rien_n_est_calcule(self):
        c = mlx_engine.Cancel()
        c.set()
        with self.assertRaises(mlx_engine.Cancelled):
            mlx_engine.transcribe([0.0], language="fond", cancel=c, _timeout=10)

    def test_arret_explicite_rapide(self):
        c, box = mlx_engine.Cancel(), {}
        th = self._background(c, box)
        time.sleep(0.2)
        t = time.monotonic()
        c.set()
        th.join(3)
        self.assertEqual(box["issue"], "interrompu")
        self.assertLess(time.monotonic() - t, 0.2)

    def test_une_dictee_n_est_jamais_interruptible(self):
        with self.assertRaises(ValueError):
            mlx_engine.transcribe([0.0], cancel=mlx_engine.Cancel(),
                                  _prio=mlx_engine.PRIO_DICTATION)

    def test_interruption_n_est_pas_un_gel(self):
        # Une interruption n'est ni un timeout ni un échec : aucun gel compté.
        before = list(mlx_engine._gpu_strikes)
        box = {}
        th = self._background(mlx_engine.Cancel(), box)
        time.sleep(0.1)
        mlx_engine.transcribe([0.0], language="dictee",
                              _prio=mlx_engine.PRIO_DICTATION, _timeout=10)
        th.join(3)
        self.assertEqual(mlx_engine._gpu_strikes, before)


@unittest.skipIf(mx is None, "MLX indisponible")
class CheckpointTests(unittest.TestCase):
    """Points de contrôle de l'encodeur et du décodeur : même sortie, arrêt net."""

    def setUp(self):
        mx.random.seed(3)
        self.enc = AudioEncoder(8, 12, 32, 4, 2, dtype=mx.float32)
        self.dec = TextDecoder(50, 16, 32, 4, 2, dtype=mx.float32)
        self.x = mx.random.normal((1, 24, 8))
        self.tok = mx.array([[1, 2, 3]])
        self.xa = self.enc(self.x)
        self.ref = self.dec(self.tok, self.xa)[0]
        mx.eval(self.xa, self.ref)
        self.model = types.SimpleNamespace(encoder=self.enc, decoder=self.dec)
        mlx_engine._forget_encoding()

    def tearDown(self):
        mlx_engine._job_cancel = None
        mlx_engine._forget_encoding()

    def test_decodeur_meme_sortie_et_branchement_idempotent(self):
        mlx_engine._accelerate(self.model)
        cls = type(self.dec)
        mlx_engine._accelerate(self.model)
        self.assertIs(type(self.dec), cls)
        self.assertTrue(issubclass(cls, TextDecoder))
        out = self.dec(self.tok, self.xa)[0]
        self.assertTrue(mx.allclose(out, self.ref, atol=1e-5).item())

    def test_jeton_leve_arrete_encodeur_et_decodeur(self):
        mlx_engine._accelerate(self.model)
        c = mlx_engine.Cancel()
        mlx_engine._job_cancel = c
        self.dec(self.tok, self.xa)                    # jeton baissé : calcul normal
        c.set()
        with self.assertRaises(mlx_engine.Cancelled):
            self.dec(self.tok, self.xa)
        with self.assertRaises(mlx_engine.Cancelled):
            self.enc(self.x + 1.0)


if __name__ == "__main__":
    unittest.main()
