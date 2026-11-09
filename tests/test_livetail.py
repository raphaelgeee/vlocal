"""Découpeur de dictée (livetail.py, v1.7) : pauses, fusion des morceaux courts,
invalidation, relâchement, garde-fous. Audio synthétique, moteur simulé : aucun
modèle chargé, rien d'écrit sur disque."""
import os
import sys
import threading
import time
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import livetail  # noqa: E402
import mlx_engine  # noqa: E402
from livetail import DictationLiveTail  # noqa: E402

SR = 16000


def speech(seconds, seed=0):
    rng = np.random.default_rng(seed)
    return (rng.standard_normal(int(seconds * SR)) * 0.1).astype(np.float32)


def quiet(seconds, seed=1):
    """Pause réaliste : bruit de fond léger (-52 dBFS), pas du zéro numérique."""
    rng = np.random.default_rng(seed)
    return (rng.standard_normal(int(seconds * SR)) * 0.0025).astype(np.float32)


def dictee(*parts):
    """('p', s) = parole, ('q', s) = pause."""
    return np.concatenate([speech(s, i) if k == "p" else quiet(s, i)
                           for i, (k, s) in enumerate(parts)])


class FakeEngine:
    """Capture qui avance à la main ; transcription simulée, interruptible.
    Le texte rendu est la durée du morceau et le contexte reçu."""

    def __init__(self, audio, hold=False, fail=None):
        self.audio = np.asarray(audio, dtype=np.float32)
        self.n = 0
        self.hold = hold                    # True : le calcul attend release
        self.release = threading.Event()
        self.fail = fail
        self.chunks = []                    # durées des morceaux envoyés (s)
        self.contexts = []

    def recorded_samples(self):
        return self.n

    def snapshot_audio(self, a, b):
        return self.audio[a:min(b, self.n)].copy()

    def transcribe_chunk(self, x, cancel=None, context=None):
        self.chunks.append(len(x) / SR)
        self.contexts.append(context)
        if self.fail is not None:
            raise self.fail
        while self.hold and not self.release.wait(0.005):
            if cancel is not None and cancel.is_set():
                raise mlx_engine.Cancelled()
        if cancel is not None and cancel.is_set():
            raise mlx_engine.Cancelled()
        return f"[{len(x) / SR:.1f}]"


def make(audio, spec=True, **kw):
    eng = FakeEngine(audio, **kw)
    lt = DictationLiveTail(eng, autostart=False)
    lt._speculation_allowed = lambda: spec
    return lt, eng


def play(lt, eng, until_s=None):
    """Simule la capture en temps réel par pas de STEP_S, sans horloge."""
    end = len(eng.audio) if until_s is None else min(len(eng.audio), int(until_s * SR))
    step = int(lt.STEP_S * SR)
    while eng.n < end:
        eng.n = min(end, eng.n + step)
        with lt._lock:
            lt._feed_until(eng.n)
            lt._tick(lt._speculation_allowed())
            job = lt._job
        if job is not None and not eng.hold:
            job.done.wait(1.0)


class PauseTruthTable(unittest.TestCase):
    """Table de vérité : 6 s de parole, une pause de durée donnée, 6 s de parole."""

    def outcome(self, pause_s):
        lt, eng = make(dictee(("p", 6), ("q", pause_s), ("p", 6)))
        play(lt, eng)
        return lt.stats, lt._pos * lt._win / SR

    def test_pause_courte_ne_lance_rien(self):
        for p in (0.2, 0.3):
            stats, pos = self.outcome(p)
            self.assertEqual(stats["lances"], 0, p)
            self.assertEqual(pos, 0.0)

    def test_pause_moyenne_lance_puis_jette(self):
        for p in (0.45, 0.6):
            stats, pos = self.outcome(p)
            self.assertEqual((stats["lances"], stats["valides"], stats["jetes"]), (1, 0, 1), p)
            self.assertEqual(pos, 0.0)

    def test_vraie_pause_valide_le_morceau(self):
        for p in (0.8, 1.0, 3.0, 5.0):
            stats, pos = self.outcome(p)
            self.assertEqual(stats["valides"], 1, p)
            # coupe dans la pause, LAUNCH_PAUSE_S / 2 après le dernier mot
            self.assertAlmostEqual(pos, 6.0 + DictationLiveTail.LAUNCH_PAUSE_S / 2, delta=0.06)

    def test_coupe_jamais_dans_la_voix(self):
        audio = dictee(("p", 6), ("q", 1.0), ("p", 6))
        lt, eng = make(audio)
        play(lt, eng)
        cut = lt._pos * lt._win
        win = audio[cut - 2400:cut + 2400]              # 150 ms de part et d'autre
        self.assertLess(float(np.sqrt(np.mean(win * win))), 0.012)


class MergeAndGuards(unittest.TestCase):
    def test_morceau_court_fusionne_avec_le_suivant(self):
        # 3 s puis pause d'1 s : trop court pour être validé seul ; le morceau
        # suivant (3 + 5 s) l'est, d'un seul tenant.
        lt, eng = make(dictee(("p", 3), ("q", 1.0), ("p", 5), ("q", 1.0), ("p", 2)))
        play(lt, eng)
        self.assertEqual(lt.stats["valides"], 1)
        self.assertEqual(len(lt._texts), 1)
        self.assertAlmostEqual(lt._pos * lt._win / SR, 9.2, delta=0.06)
        self.assertGreater(eng.chunks[-1], 9.0)       # un seul morceau couvre les deux

    def test_quatre_mots_apres_la_pause_restent_a_la_fin(self):
        audio = dictee(("p", 8), ("q", 1.5), ("p", 1.2), ("q", 0.15))
        lt, eng = make(audio)
        play(lt, eng)
        texts, pos = lt.stop_and_collect(audio)
        self.assertEqual(len(texts), 1)
        self.assertAlmostEqual(pos / SR, 8.2, delta=0.06)     # la fin reste à transcrire

    def test_contexte_du_morceau_suivant(self):
        audio = dictee(("p", 5), ("q", 1.0), ("p", 5), ("q", 1.0), ("p", 5), ("q", 1.0))
        lt, eng = make(audio)
        play(lt, eng)
        self.assertEqual(eng.contexts[0], None)
        self.assertEqual(eng.contexts[1], "[5.2]")
        self.assertEqual(eng.contexts[2], "[5.2] [6.0]")
        self.assertEqual(lt.context(), "[5.2] [6.0]")

    def test_contexte_borne(self):
        lt, _ = make(speech(1))
        lt._texts = ["mot " * 500]
        self.assertLessEqual(len(lt.context()), DictationLiveTail.CONTEXT_CHARS)

    def test_dictee_de_moins_de_2_s_sans_speculatif(self):
        audio = dictee(("p", 1.8), ("q", 2.0))
        lt, eng = make(audio)
        play(lt, eng)
        self.assertEqual(eng.chunks, [])
        self.assertEqual(lt.stop_and_collect(audio), ([], 0))

    def test_morceau_sans_voix_jamais_envoye(self):
        audio = quiet(12.0)
        lt, eng = make(audio)
        play(lt, eng)
        self.assertEqual(eng.chunks, [])
        self.assertEqual(lt.stop_and_collect(audio), ([], 0))

    def test_clic_isole_ne_rouvre_pas_la_voix(self):
        audio = dictee(("p", 5), ("q", 1.0))
        audio[int(5.6 * SR):int(5.63 * SR)] = 0.3     # clic de 30 ms
        lt, eng = make(audio)
        play(lt, eng)
        texts, pos = lt.stop_and_collect(audio)
        self.assertEqual((len(texts), pos), (1, len(audio)))


class ReleaseAndInvalidation(unittest.TestCase):
    def test_pause_avant_relachement_texte_deja_pret(self):
        audio = dictee(("p", 5), ("q", 1.2))
        lt, eng = make(audio)
        play(lt, eng)
        texts, pos = lt.stop_and_collect(audio)
        self.assertEqual(texts, ["[5.2]"])
        self.assertEqual(pos, len(audio))              # rien d'autre à transcrire
        self.assertEqual(lt.stats["final"], 1)

    def test_calcul_en_vol_attendu_s_il_rend_le_texte_final(self):
        audio = dictee(("p", 5), ("q", 0.8))
        lt, eng = make(audio, hold=True)
        play(lt, eng)
        threading.Timer(0.05, eng.release.set).start()
        texts, pos = lt.stop_and_collect(audio)
        self.assertEqual((texts, pos), (["[5.2]"], len(audio)))

    def test_voix_revenue_le_calcul_en_vol_est_interrompu(self):
        audio = dictee(("p", 5), ("q", 0.5), ("p", 2))
        lt, eng = make(audio, hold=True)
        play(lt, eng)                                  # pause trop courte : jeté
        self.assertEqual(lt.stats["jetes"], 1)
        self.assertIsNone(lt._job)
        self.assertEqual(lt.stop_and_collect(audio), ([], 0))

    def test_relachement_en_pleine_voix_interrompt_sans_attendre(self):
        audio = dictee(("p", 5), ("q", 0.6), ("p", 0.3))
        lt, eng = make(audio, hold=True)
        play(lt, eng, until_s=5.55)
        job = lt._job
        self.assertIsNotNone(job)
        texts, pos = lt.stop_and_collect(audio)        # voix après la coupe, pause < 0,7 s
        self.assertTrue(job.cancel.is_set())
        self.assertTrue(job.done.wait(1.0))
        self.assertIsInstance(job.error, mlx_engine.Cancelled)
        self.assertEqual((texts, pos), ([], 0))

    def test_resultat_tardif_ignore_apres_le_relachement(self):
        # Course avec la finalisation : une fois stop_and_collect rendu, aucun
        # calcul ne modifie plus l'état (la livraison passe par claim() seul).
        audio = dictee(("p", 6), ("q", 1.0), ("p", 6))
        lt, eng = make(audio, hold=True)
        play(lt, eng, until_s=6.8)
        job = lt._job
        lt.JOIN_S = 0.05
        texts, pos = lt.stop_and_collect(audio[:int(6.8 * SR)])
        eng.release.set()
        job.done.wait(1.0)
        with lt._lock:
            lt._tick(True)
        self.assertEqual((lt._texts, lt._pos), ([], 0))
        self.assertEqual((texts, pos), ([], 0))
        self.assertTrue(job.cancel.is_set())

    def test_echec_gpu_arrete_l_anticipation(self):
        audio = dictee(("p", 5), ("q", 1.0), ("p", 5), ("q", 1.0))
        lt, eng = make(audio, fail=RuntimeError("Metal"))
        play(lt, eng)
        self.assertEqual(lt.stats["lances"], 1)
        self.assertTrue(lt._broken)
        self.assertEqual(lt.stop_and_collect(audio), ([], 0))

    def test_interrompu_par_une_dictee_la_pause_est_reprise(self):
        audio = dictee(("p", 5), ("q", 1.5))
        lt, eng = make(audio, hold=True)
        play(lt, eng, until_s=5.6)
        lt._job.cancel.set()                           # une dictée passe devant
        lt._job.done.wait(1.0)
        eng.hold = False
        play(lt, eng)
        self.assertEqual(lt.stats["lances"], 2)
        texts, pos = lt.stop_and_collect(audio)
        self.assertEqual(pos, len(audio))
        self.assertEqual(len(texts), 1)

    def test_abort_coupe_tout(self):
        audio = dictee(("p", 5), ("q", 1.0))
        lt, eng = make(audio, hold=True)
        play(lt, eng)
        job = lt._job
        lt.abort()
        self.assertTrue(job.cancel.is_set())
        self.assertTrue(job.done.wait(1.0))

    def test_thread_reel_s_arrete(self):
        audio = dictee(("p", 3), ("q", 1.0))
        eng = FakeEngine(audio)
        eng.n = len(audio)
        lt = DictationLiveTail(eng)
        lt._speculation_allowed = lambda: True
        lt.stop_and_collect(audio)
        self.assertFalse(lt._thr.is_alive())


    def test_sonde_gpu_lente_ne_retient_pas_le_relachement(self):
        audio = dictee(("p", 3), ("q", 1.0))
        eng = FakeEngine(audio)
        eng.n = len(audio)
        lt = DictationLiveTail(eng)
        entered = threading.Event()

        def slow_probe():
            entered.set()
            threading.Event().wait(2.0)     # 1re sonde GPU de la session
            return False
        lt._speculation_allowed = slow_probe
        self.assertTrue(entered.wait(1.0))
        t = time.monotonic()
        self.assertEqual(lt.stop_and_collect(audio), ([], 0))
        self.assertLess(time.monotonic() - t, 1.5)


class WithoutSpeculation(unittest.TestCase):
    """GPU absent ou mémoire sous pression : seules restent les fenêtres longues
    historiques (TARGET_S après la dernière coupe, armement à ARM_S)."""

    def test_pauses_ignorees_avant_la_cible(self):
        audio = dictee(("p", 10), ("q", 1.0), ("p", 20))
        lt, eng = make(audio, spec=False)
        play(lt, eng)
        self.assertEqual(eng.chunks, [])

    def test_silence_au_dela_de_la_cible_coupe(self):
        audio = dictee(("p", 50), ("q", 1.0), ("p", 9))
        lt, eng = make(audio, spec=False)
        play(lt, eng)
        self.assertEqual(lt.stats["valides"], 1)
        self.assertAlmostEqual(lt._pos * lt._win / SR, 50.2, delta=0.06)

    def test_sans_silence_aucune_coupe(self):
        lt, eng = make(speech(90), spec=False)
        play(lt, eng)
        self.assertEqual(eng.chunks, [])

    def test_politique_reelle_suit_gpu_et_memoire(self):
        lt, _ = make(speech(1))
        del lt._speculation_allowed
        old = (livetail.mlx_engine.available, livetail.memory_policy.pressure_level)
        try:
            livetail.mlx_engine.available = lambda: True
            livetail.memory_policy.pressure_level = lambda: livetail.memory_policy.NORMAL
            self.assertTrue(lt._speculation_allowed())
            livetail.memory_policy.pressure_level = lambda: livetail.memory_policy.WARN
            self.assertFalse(lt._speculation_allowed())
            livetail.mlx_engine.available = lambda: False
            livetail.memory_policy.pressure_level = lambda: livetail.memory_policy.NORMAL
            self.assertFalse(lt._speculation_allowed())
        finally:
            livetail.mlx_engine.available, livetail.memory_policy.pressure_level = old


class VoiceTrackTests(unittest.TestCase):
    def test_lecture_incrementale_identique_a_la_lecture_d_un_bloc(self):
        audio = dictee(("p", 2), ("q", 0.7), ("p", 1))
        a, b = livetail._VoiceTrack(), livetail._VoiceTrack()
        a.feed(audio)
        for i in range(0, len(audio), 777):
            b.feed(audio[i:i + 777])
        self.assertEqual(a.n, b.n)
        self.assertTrue(np.array_equal(a._voiced[:a.n], b._voiced[:b.n]))
        self.assertAlmostEqual(a.speech_s(0, a.n), 3.0, delta=0.06)

    def test_capacite_etendue_sans_perte(self):
        t = livetail._VoiceTrack()
        t.feed(dictee(("p", 200), ("q", 1.0)))       # > 4096 fenêtres
        self.assertAlmostEqual(t.speech_s(0, t.n), 200.0, delta=0.06)
        self.assertIsNotNone(t.pause_from(0, 20))

    def test_stop_and_collect_sans_thread(self):
        lt, _ = make(speech(1))
        self.assertEqual(lt.stop_and_collect(), ([], 0))


if __name__ == "__main__":
    unittest.main()
