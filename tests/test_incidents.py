"""v1.4.4 — Comptage honnête des incidents de dictée.

- un appui raté = UN incident, avec l'étape en cause (errors.MicStartTrace) ;
- un texte vide est classé d'après le moteur (errors.empty_text_code) ;
- le chien de garde de finalisation suit le budget du moteur, dit la dictée
  perdue et la journalise ;
- le verdict GPU est oublié après le téléchargement du modèle.

app.py ne s'importe pas dans un test (pywebview, AppKit, moteur) : les quelques
définitions visées en sont extraites telles quelles (ast) et exécutées dans un
espace de noms de doublures. C'est bien le code livré qui est testé.
"""
import ast
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

import errors  # noqa: E402

E = errors.E
APP = os.path.join(RACINE, "app.py")


def _source_app():
    with open(APP, encoding="utf-8") as f:
        return f.read()


def _arbre_app():
    return ast.parse(_source_app(), APP)


def _charge_depuis_app(noms, espace):
    """Exécute dans `espace` les définitions de premier niveau de app.py nommées
    dans `noms` (fonctions, classes, affectations simples), dans leur ordre."""
    arbre = _arbre_app()
    choisis = []
    for n in arbre.body:
        nom = None
        if isinstance(n, (ast.FunctionDef, ast.ClassDef)):
            nom = n.name
        elif isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
            nom = n.targets[0].id
        if nom in noms:
            choisis.append(n)
    trouves = {getattr(n, "name", None) or n.targets[0].id for n in choisis}
    manquants = set(noms) - trouves
    if manquants:
        raise AssertionError(f"définitions absentes de app.py : {sorted(manquants)}")
    exec(compile(ast.Module(body=choisis, type_ignores=[]), APP, "exec"), espace)
    return espace


def _litteral_app(nom):
    for n in _arbre_app().body:
        if isinstance(n, (ast.Assign, ast.AnnAssign)):
            cible = n.targets[0] if isinstance(n, ast.Assign) else n.target
            if isinstance(cible, ast.Name) and cible.id == nom:
                return ast.literal_eval(n.value)
    raise AssertionError(f"{nom} absent de app.py")


class _JournalCapture:
    """Remplace errors.log le temps d'un test et garde les événements."""

    def __enter__(self):
        self.evts = []
        self._p = mock.patch.object(errors, "log",
                                    lambda code, level="error", **ctx: self.evts.append((code, level, ctx)))
        self._p.start()
        return self

    def __exit__(self, *exc):
        self._p.stop()


class TestUnAppuiUnIncident(unittest.TestCase):
    """Critère 3 : l'escalade complète d'un appui raté = exactement 1 incident."""

    def test_escalade_complete_en_echec(self):
        with _JournalCapture() as j:
            t = errors.MicStartTrace()
            t.fail("start", OSError("PortAudio -9986"))
            t.fail("start_retry", RuntimeError("budget épuisé"))
            t.fail("fallback_builtin", OSError("x" * 500))
            t.fail("fallback_exhausted")
            t.log_outcome(False, None, device=None)
        self.assertEqual(len(j.evts), 1)
        code, level, ctx = j.evts[0]
        self.assertEqual((code, level), (E.MIC_DEVICE_FAIL, "error"))
        self.assertEqual(ctx["where"], "fallback_exhausted")
        self.assertEqual(ctx["steps"], "start,start_retry,fallback_builtin,fallback_exhausted")

    def test_echec_au_premier_essai_seul(self):
        t = errors.MicStartTrace()
        t.fail("start", OSError("occupé"))
        code, _, ctx = t.outcome(False)
        self.assertEqual((code, ctx["where"], ctx["err"]), (E.MIC_DEVICE_FAIL, "start", "occupé"))

    def test_recuperation_compte_une_fois_sans_echec(self):
        for via, etapes in (("hard_reset", ["start"]),
                            ("builtin", ["start", "start_retry"]),
                            ("device", ["start", "start_retry", "fallback_builtin"])):
            with _JournalCapture() as j:
                t = errors.MicStartTrace()
                for e in etapes:
                    t.fail(e, "err")
                t.log_outcome(True, via, device="Micro USB" if via == "device" else None)
            self.assertEqual(len(j.evts), 1, via)
            code, level, ctx = j.evts[0]
            self.assertEqual((code, level, ctx["where"]), (E.MIC_RECOVERED, "info", via))
            self.assertEqual(ctx["after"], ",".join(etapes))

    def test_aucun_echec_aucun_incident(self):
        with _JournalCapture() as j:
            errors.MicStartTrace().log_outcome(True, None)
            errors.MicStartTrace().log_outcome(False, None)
        self.assertEqual(j.evts, [])

    def test_app_ne_journalise_plus_chaque_etape(self):
        """Garde-fou : dans app.py, l'escalade passe par la trace ; les seuls
        mic_device_fail directs restants sont des appuis distincts (flux mort
        pendant l'enregistrement, verrou de démarrage figé)."""
        src = _source_app()
        import re
        directs = re.findall(r"_EV\.log\(_EV\.E\.MIC_DEVICE_FAIL,\s*where=\"(\w+)\"", src)
        self.assertEqual(sorted(directs), ["start_lock_stuck", "stream_dead"])
        self.assertNotIn("_EV.log(_EV.E.MIC_RECOVERED", src)
        etapes = re.findall(r"_trace\.fail\(\"(\w+)\"", src)
        self.assertEqual(etapes, ["start", "start_retry", "fallback_builtin", "fallback_exhausted"])
        self.assertEqual(src.count("_trace.log_outcome("), 1)


class TestTexteVide(unittest.TestCase):
    """Critère 5 : table de vérité du classement d'un texte vide."""

    SIGNAL = (0.02, 0.2)      # voix audible
    NUL = (0.0002, 0.001)     # rien du micro

    def test_table_de_verite(self):
        attendu = [
            ("gate", self.SIGNAL, E.SILENCE),
            (None, self.SIGNAL, E.SILENCE),
            ("vad", self.SIGNAL, E.NO_SPEECH),
            ("filtered", self.SIGNAL, E.NO_SPEECH),
            ("fail", self.SIGNAL, E.TRANSCRIBE_FAIL),
            ("fail", self.NUL, E.TRANSCRIBE_FAIL),
            ("gate", self.NUL, E.MIC_ZERO_SIGNAL),
            ("vad", self.NUL, E.MIC_ZERO_SIGNAL),
            (None, self.NUL, E.MIC_ZERO_SIGNAL),
        ]
        for raison, (rms, pic), code in attendu:
            self.assertEqual(errors.empty_text_code(raison, rms, pic), code, (raison, rms, pic))

    def test_signal_nul_n_est_pas_une_panne_de_peripherique(self):
        self.assertNotEqual(errors.empty_text_code("gate", 0.0, 0.0), E.MIC_DEVICE_FAIL)
        # Un pic audible suffit : ce n'est plus un signal nul.
        self.assertEqual(errors.empty_text_code("gate", 0.0005, 0.05), E.SILENCE)

    def test_chaque_code_a_ses_messages_traduits(self):
        msgs = _litteral_app("_EMPTY_TEXT_MSG")
        codes = {E.SILENCE, E.NO_SPEECH, E.TRANSCRIBE_FAIL, E.MIC_ZERO_SIGNAL}
        self.assertEqual(set(msgs), codes)
        bt = _litteral_app("_BT")
        textes = [t for paire in msgs.values() for t in paire]
        textes += [_litteral_app("_LOST_DICTATION_TOAST"), _litteral_app("_LOST_DICTATION_OVERLAY")]
        for t in textes:
            self.assertTrue(bt.get(t), f"pas de traduction anglaise : {t!r}")
            self.assertNotIn("—", t + bt[t])
        self.assertNotIn("très faible", msgs[E.TRANSCRIBE_FAIL][0] + msgs[E.NO_SPEECH][0])


class _FauxMoteur(types.ModuleType):
    def __init__(self, budget):
        super().__init__("engine")
        self.appels = []
        self._budget = budget

    def finalize_budget_s(self, audio_s):
        self.appels.append(audio_s)
        return self._budget(audio_s)


class TestBudgetDeFinalisation(unittest.TestCase):
    """Critère 7 : budget = engine.finalize_budget_s + marge, plancher conservé."""

    def _budget(self, f_moteur):
        moteur = _FauxMoteur(f_moteur)
        ns = _charge_depuis_app({"FINALIZE_WATCHDOG_S", "FINALIZE_MARGIN_S", "_finalize_budget_s"},
                                {"_engine_mod": moteur})
        return ns, moteur

    def test_suit_le_budget_du_moteur(self):
        ns, moteur = self._budget(lambda a: 20.0 + 0.5 * a)
        marge = ns["FINALIZE_MARGIN_S"]
        self.assertGreater(marge, 0.0)
        self.assertLessEqual(marge, 5.0)
        for audio in (60.0, 240.0, 600.0):
            self.assertAlmostEqual(ns["_finalize_budget_s"](audio), 20.0 + 0.5 * audio + marge)
        self.assertEqual(moteur.appels, [60.0, 240.0, 600.0])

    def test_plancher_pour_les_dictees_courtes(self):
        ns, _ = self._budget(lambda a: 4.0)
        self.assertEqual(ns["_finalize_budget_s"](2.0), ns["FINALIZE_WATCHDOG_S"])

    def test_le_repli_cpu_n_est_plus_ampute(self):
        """L'ancien budget « timeout GPU + 3 s » ne laissait rien au repli CPU."""
        ns, _ = self._budget(lambda a: 20.0 + 0.5 * a)
        gpu_seul = 12.0 + 0.1 * 600.0     # ordre de grandeur du seul timeout GPU
        self.assertGreater(ns["_finalize_budget_s"](600.0), gpu_seul + 3.0)
        self.assertNotIn("dictation_gpu_timeout", _source_app())


class TestChienDeGarde(unittest.TestCase):
    """Le chien de garde déclenché : journalisé, dit à l'utilisateur, et le
    résultat tardif n'est pas rendu pour insertion."""

    def setUp(self):
        self.evts, self.toasts, self.overlays = [], [], []
        ev = types.SimpleNamespace(E=E, log=lambda code, level="error", **ctx: self.evts.append((code, ctx)))
        self.ns = _charge_depuis_app(
            {"_DictationStop", "DictationController", "_LOST_DICTATION_TOAST",
             "_LOST_DICTATION_OVERLAY"},
            {"threading": threading, "time": time, "VlocalEngine": object, "_EV": ev,
             "_finalize_budget_s": lambda d: 0.3,
             "_end_activity": lambda tok: None,
             "_te": lambda s: s,
             "_toast": lambda m, kind="info": self.toasts.append(m),
             "_schedule_overlay_hide": lambda d: None,
             "overlay": types.SimpleNamespace(error=lambda m: self.overlays.append(m))})

    def _controleur(self, duree_finalisation, texte="bonjour"):
        Base = self.ns["DictationController"]

        class Ctrl(Base):
            def _do_finalize(self, *a, **k):
                time.sleep(duree_finalisation)
                return texte

        c = Ctrl.__new__(Ctrl)
        c._lock = threading.Lock()
        c._active = True
        c._stop_evt = self.ns["_DictationStop"]()
        c._audio_worker = c._tail = c._activity = None
        c._t0 = time.time() - 3.0
        c._fin_cv = threading.Condition()
        c._fin_next = c._fin_serving = c._fin_inflight = c._epoch = 0
        c._busy = False
        c._outcome = threading.local()
        return c

    def test_finalisation_normale_inchangee(self):
        c = self._controleur(0.05)
        self.assertEqual(c.stop_and_finalize(notify_ui=False), "bonjour")
        self.assertIsNone(c.empty_overlay_text())
        time.sleep(0.4)
        self.assertEqual((self.evts, self.toasts), ([], []))
        self.assertFalse(c._busy)

    def test_declenche_dit_et_journalise_la_perte(self):
        c = self._controleur(0.8)
        self.assertEqual(c.stop_and_finalize(notify_ui=False), "")
        self.assertEqual([e[0] for e in self.evts], [E.FINALIZE_TIMEOUT])
        self.assertEqual(self.toasts, [self.ns["_LOST_DICTATION_TOAST"]])
        self.assertEqual(self.overlays, [self.ns["_LOST_DICTATION_OVERLAY"]])
        self.assertEqual(c.empty_overlay_text(), self.ns["_LOST_DICTATION_OVERLAY"])
        self.assertNotIn("réparée", self.toasts[0] + self.overlays[0])
        self.assertEqual((c._fin_inflight, c._busy, c._fin_serving), (0, False, 1))


class TestVerdictGpuApresTelechargement(unittest.TestCase):
    """Critère 6 : _finish_model_download repointe le moteur GPU sur le cache,
    puis oublie le verdict GPU (reset_availability garde une référence de
    modèle qui existe : contrat livré par le lot GPU)."""

    def test_reset_availability_apres_repointage(self):
        ordre = []
        mlx = types.ModuleType("mlx_engine")
        mlx.reset_availability = lambda raison: ordre.append(("reset", raison))
        mlx.available = mock.Mock(side_effect=AssertionError("pas de sonde ici"))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        open(os.path.join(tmp.name, "model.bin"), "wb").close()
        fil = mock.Mock()
        ns = _charge_depuis_app({"_finish_model_download"}, {
            "os": os, "MODEL_DIR": tmp.name, "SMALL_MODEL_DIR": "/nope",
            "_cpu_threads_resolved": 4, "_engine_load_params": None,
            "_resolve_model_dirs": lambda: ordre.append(("resolve",)),
            "_point_mlx_to_cache": lambda: ordre.append(("point",)),
            "_load_engine_async": lambda: None,
            "threading": types.SimpleNamespace(Thread=fil)})
        with mock.patch.dict(sys.modules, {"mlx_engine": mlx}):
            self.assertTrue(ns["_finish_model_download"]())
        self.assertEqual(ordre, [("resolve",), ("point",), ("reset", "modèle téléchargé")])
        mlx.available.assert_not_called()
        self.assertEqual(ns["_engine_load_params"], (tmp.name, 4))
        fil.return_value.start.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
