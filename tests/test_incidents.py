"""v1.4.4 — Comptage honnête des incidents de dictée.

- un appui raté = UN incident, avec l'étape en cause (errors.MicStartTrace) ;
- un texte vide est classé d'après le moteur (errors.empty_text_code) ;
- le chien de garde de finalisation suit le budget du moteur, dit la dictée
  perdue et la journalise ;
- le verdict GPU est oublié après le téléchargement du modèle.

Les définitions visées sont extraites des sources de l'app telles quelles (ast) et exécutées dans un
espace de noms de doublures. C'est bien le code livré qui est testé.
"""
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

import app_sources  # noqa: E402
import errors  # noqa: E402

E = errors.E


def _source_app():
    return app_sources.texte_complet()


def _charge_depuis_app(noms, espace):
    """Exécute dans `espace` les définitions de premier niveau de l'app (app.py et
    ses modules) nommées dans `noms` (fonctions, classes, affectations simples)."""
    return app_sources.charge(noms, espace)


def _litteral_app(nom):
    return app_sources.litteral(nom)


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


class _FauxMicro:
    """Moteur dont l'ouverture du micro échoue selon un scénario : `pannes` =
    ensemble des tentatives qui lèvent ("start", "builtin", "device:A"...)."""

    def __init__(self, pannes, devices=("A", "B")):
        self.pannes = set(pannes)
        self.devices = list(devices)
        self.tentatives = []
        self.resets = 0

    def start_recording(self, prefer=None, device=None, deadline=None):
        nom = "builtin" if prefer == "builtin" else (f"device:{device}" if device else "start")
        n = sum(1 for t in self.tentatives if t == nom)
        self.tentatives.append(nom)
        cle = f"{nom}#{n}" if f"{nom}#{n}" in self.pannes else nom
        if cle in self.pannes:
            raise OSError(f"PortAudio : {nom} indisponible")
        return True

    def hard_mic_reset(self, timeout=None):
        self.resets += 1
        return True

    def list_input_devices(self):
        return self.devices

    # Chemin « micro ouvert » de start()
    def rms_recent(self, s):
        return 0.0

    def seconds_since_audio(self):
        return 0.0

    def prewarm(self, want_small=False):
        pass


class TestEscaladeReelle(unittest.TestCase):
    """Le VRAI DictationController.start de app.py, micro simulé : chaque issue
    de l'escalade donne exactement un événement."""

    def _appui(self, pannes):
        ns = {"threading": threading, "time": time, "VlocalEngine": object, "_EV": errors,
              "_mic_fail_count": [0], "_mic_grabber_name": lambda: None,
              "_toast": lambda m, kind="info": None, "_ui": lambda code: None,
              "overlay": types.SimpleNamespace(level=lambda r: None, info=lambda *a: None),
              "_te": lambda t: t, "_begin_activity": lambda nom: None,
              "_load_settings": lambda: {"dictation_speed": "rapide"},
              "_DictationLiveTail": None}
        _charge_depuis_app({"_time_left", "_DictationStop", "DictationController"}, ns)
        moteur = _FauxMicro(pannes)
        c = ns["DictationController"](moteur)
        with _JournalCapture() as j:
            ok = c.start()
        c._stop_evt.set()
        return ok, j.evts, moteur

    def test_echec_complet_un_seul_incident(self):
        ok, evts, moteur = self._appui({"start", "builtin", "device:A", "device:B"})
        self.assertFalse(ok)
        self.assertEqual(moteur.tentatives, ["start", "start", "builtin", "device:A", "device:B"])
        self.assertEqual(len(evts), 1)
        code, level, ctx = evts[0]
        self.assertEqual((code, ctx["where"]), (E.MIC_DEVICE_FAIL, "fallback_exhausted"))
        self.assertEqual(ctx["steps"], "start,start_retry,fallback_builtin,fallback_exhausted")

    def test_recuperations_une_seule_entree(self):
        cas = [({"start#0"}, "hard_reset"),
               ({"start"}, "builtin"),
               ({"start", "builtin", "device:A"}, "device")]
        for pannes, via in cas:
            ok, evts, _ = self._appui(pannes)
            self.assertTrue(ok, via)
            self.assertEqual([(e[0], e[2]["where"]) for e in evts], [(E.MIC_RECOVERED, via)])
            self.assertIn(evts[0][2]["where"], errors.WHERE_CODES)

    def test_micro_sain_aucun_incident(self):
        ok, evts, _ = self._appui(set())
        self.assertTrue(ok)
        self.assertEqual(evts, [])

    def test_etapes_journalisees_dans_la_liste_fermee(self):
        """Toute étape `where` écrite dans app.py est connue de la télémétrie
        (sinon elle ne serait jamais transmise, en silence)."""
        import re
        src = _source_app()
        ecrites = set(re.findall(r"where=\"(\w+)\"", src)) | set(re.findall(r"_trace\.fail\(\"(\w+)\"", src))
        ecrites |= set(re.findall(r"_via(?:, _dev_ok)? = \"(\w+)\"", src))
        self.assertTrue(ecrites)
        self.assertLessEqual(ecrites, errors.WHERE_CODES)


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
        textes += [_litteral_app(n) for n in (
            "_LOST_DICTATION_TOAST", "_LOST_DICTATION_OVERLAY", "_LATE_PENDING_TOAST",
            "_LATE_PENDING_OVERLAY", "_LATE_SAVED_TOAST", "_LATE_SAVED_OVERLAY")]
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


class _FauxMoteurDictee:
    """VlocalEngine simulé : chaque transcription consomme un scénario
    (durée, texte | exception, raison du vide)."""

    sample_rate = 16000

    def __init__(self, scenarios):
        self._scenarios = list(scenarios)
        self._raison = None
        self.last_rms, self.last_peak = 0.02, 0.2
        self.transcriptions = 0

    def stop_recording(self):
        return [0.0] * (self.sample_rate * 3)

    def stop_and_transcribe(self, **kw):
        duree, issue, raison = self._scenarios.pop(0)
        self.transcriptions += 1
        time.sleep(duree)
        self._raison = raison
        if isinstance(issue, BaseException):
            raise issue
        return issue

    def last_empty_reason(self):
        return self._raison

    def last_backend(self):
        return "cpu"


class _FauxStore:
    def __init__(self):
        self.dictees, self.usages = [], []

    def glossary_pairs(self):
        return []

    def snippet_pairs(self):
        return []

    def word_count(self, t):
        return len(t.split())

    def record_usage(self, mots, duree):
        self.usages.append(mots)

    def add_dictation(self, texte, mode):
        self.dictees.append(texte)


class TestFinalisationReelle(unittest.TestCase):
    """Le VRAI stop_and_finalize / _do_finalize de app.py, moteur simulé.
    Garanties : un seul incident par appui, rien d'inséré ni de copié après le
    chien de garde, le texte tardif gardé dans l'historique, et une dictée
    enchaînée derrière une dictée lente qui n'est pas déclarée perdue."""

    BUDGET = 0.6

    def _monte(self, scenarios, historique="all"):
        self.evts, self.toasts, self.overlays, self.copies = [], [], [], []
        self.store = _FauxStore()
        self.reglages = {"history_scope": historique, "dictation_speed": "auto"}
        ev = types.SimpleNamespace(
            E=E, empty_text_code=errors.empty_text_code,
            log=lambda code, level="error", **ctx: self.evts.append(code))
        faux_engine = types.ModuleType("engine")
        faux_engine._qos_user_initiated = lambda: None
        p = mock.patch.dict(sys.modules, {"engine": faux_engine})
        p.start()
        self.addCleanup(p.stop)
        import processor
        ns = {
            "threading": threading, "time": time, "re": __import__("re"),
            "processor": processor, "VlocalEngine": object, "_EV": ev,
            "_finalize_budget_s": lambda d: self.BUDGET,
            "_end_activity": lambda tok: None, "_te": lambda t: t,
            "_toast": lambda m, kind="info": self.toasts.append(m),
            "_schedule_overlay_hide": lambda d: None,
            "overlay": types.SimpleNamespace(error=lambda m: self.overlays.append(m)),
            "appstate": types.SimpleNamespace(store=self.store, telemetry=None),
            "_load_settings": lambda: self.reglages,
            "_save_settings": lambda d: None, "_current_mode": lambda: "DICTEE",
            "copy_to_clipboard": lambda t: self.copies.append(t) or True,
            "_ui": lambda code: None, "permissions": None, "_acc_warned": [False],
            "_looks_like_timed_reminder": lambda t: False,
            "_refresh_lists_ui": lambda: None, "_post_process_by_mode": lambda raw, mode: None,
        }
        _charge_depuis_app(
            {"_EMPTY_TEXT_MSG", "_LOST_DICTATION_TOAST", "_LOST_DICTATION_OVERLAY",
             "_LATE_PENDING_TOAST", "_LATE_PENDING_OVERLAY", "_LATE_SAVED_TOAST",
             "_LATE_SAVED_OVERLAY", "_polish_dictation", "_history_keeps_dictations",
             "_late_dictation_messages", "_DictationStop", "_FinalizeWatch",
             "DictationController"}, ns)
        self.ns = ns
        c = ns["DictationController"].__new__(ns["DictationController"])
        c.engine = _FauxMoteurDictee(scenarios)
        c._lock = threading.Lock()
        c._stop_evt = ns["_DictationStop"]()
        c._audio_worker = c._tail = c._activity = None
        c._fin_cv = threading.Condition()
        c._fin_next = c._fin_serving = c._fin_inflight = c._epoch = 0
        c._busy = False
        c._outcome = threading.local()
        return c

    def _relache(self, c):
        """Ce que start() pose avant un relâchement, puis le relâchement."""
        c._active = True
        c._t0 = time.time() - 3.0
        return c.stop_and_finalize(notify_ui=False)

    def test_dictee_normale(self):
        c = self._monte([(0.05, "bonjour à tous", None)])
        self.assertEqual(self._relache(c), "bonjour à tous")
        self.assertEqual(self.copies, ["bonjour à tous"])
        self.assertEqual(self.store.dictees, ["bonjour à tous"])
        time.sleep(self.BUDGET + 0.2)
        self.assertEqual((self.evts, self.toasts), ([], []))
        self.assertEqual((c._fin_inflight, c._busy), (0, False))

    def test_texte_tardif_garde_sans_insertion_ni_copie(self):
        c = self._monte([(self.BUDGET + 0.4, "texte arrivé tard", None)])
        self.assertEqual(self._relache(c), "")          # rien à insérer au curseur
        self.assertEqual(self.copies, [])                # presse-papier jamais écrasé
        self.assertEqual(self.evts, [E.FINALIZE_TIMEOUT])
        self.assertEqual(self.store.dictees, ["texte arrivé tard"])
        self.assertEqual(self.store.usages, [])          # pas compté comme dictée utile
        self.assertEqual(self.toasts, [self.ns["_LATE_PENDING_TOAST"],
                                       self.ns["_LATE_SAVED_TOAST"]])
        self.assertEqual(c.empty_overlay_text(), self.ns["_LATE_SAVED_OVERLAY"])
        self.assertEqual((c._fin_inflight, c._busy, c._fin_serving), (0, False, 1))

    def test_historique_desactive_la_dictee_est_dite_perdue(self):
        c = self._monte([(self.BUDGET + 0.4, "texte arrivé tard", None)], historique="none")
        self.assertEqual(self._relache(c), "")
        self.assertEqual((self.copies, self.store.dictees), ([], []))
        self.assertEqual(self.evts, [E.FINALIZE_TIMEOUT])
        self.assertEqual(self.toasts, [self.ns["_LOST_DICTATION_TOAST"]])
        self.assertEqual(c.empty_overlay_text(), self.ns["_LOST_DICTATION_OVERLAY"])

    def test_vide_tardif_un_seul_incident(self):
        for issue, raison in (("", "vad"), ("", "gate"), (TimeoutError("GPU"), None),
                              (RuntimeError("panne"), "fail")):
            c = self._monte([(self.BUDGET + 0.4, issue, raison)])
            self.assertEqual(self._relache(c), "")
            self.assertEqual(self.evts, [E.FINALIZE_TIMEOUT], (issue, raison))
            self.assertEqual(self.toasts, [self.ns["_LATE_PENDING_TOAST"]], (issue, raison))

    def test_echec_moteur_un_seul_transcribe_fail(self):
        for issue, raison in ((RuntimeError("décodeur KO"), "fail"), ("", "fail")):
            c = self._monte([(0.05, issue, raison)])
            self.assertEqual(self._relache(c), "")
            time.sleep(self.BUDGET + 0.2)                 # le chien de garde reste muet
            self.assertEqual(self.evts, [E.TRANSCRIBE_FAIL], issue)
            self.assertEqual(self.toasts, [self.ns["_EMPTY_TEXT_MSG"]["transcribe_fail"][0]])
            self.assertEqual(c.empty_overlay_text(),
                             self.ns["_EMPTY_TEXT_MSG"]["transcribe_fail"][1])

    def _avec_decoupeur(self, c, morceaux, pos):
        """Découpeur simulé (livetail.py) : morceaux validés pendant la parole."""
        vu = {}

        class _Decoupeur:
            stats = {"lances": 1, "valides": 1, "jetes": 0, "final": 0}

            def stop_and_collect(self, audio=None):
                vu["audio"] = audio
                return list(morceaux), pos
        c._tail = _Decoupeur()
        recu = {}
        orig = c.engine.stop_and_transcribe

        def stop_and_transcribe(**kw):
            recu.update(kw)
            return orig(**kw)
        c.engine.stop_and_transcribe = stop_and_transcribe
        return vu, recu

    def test_decoupeur_recoit_l_audio_et_ses_morceaux_sont_finalises(self):
        c = self._monte([(0.05, "début. fin.", None)])
        vu, recu = self._avec_decoupeur(c, ["début."], 16000)
        self.assertEqual(self._relache(c), "début. fin.")
        self.assertEqual(len(vu["audio"]), 3 * 16000)        # PCM complet, micro fermé
        self.assertEqual(recu["live_tail"], (["début."], 16000))
        self.assertIs(recu["audio"], vu["audio"])

    def test_morceaux_anticipes_tardifs_jamais_inseres(self):
        # Course entre l'anticipation et le chien de garde : le texte, même
        # anticipé, ne passe que par claim() ; en retard, il n'est pas inséré.
        c = self._monte([(self.BUDGET + 0.4, "début. fin.", None)])
        self._avec_decoupeur(c, ["début."], 16000)
        self.assertEqual(self._relache(c), "")
        self.assertEqual(self.copies, [])
        self.assertEqual(self.evts, [E.FINALIZE_TIMEOUT])

    def test_dictee_enchainee_derriere_une_lente_n_est_pas_perdue(self):
        # A prend presque tout son budget ; B, relâchée juste après, attend A
        # puis se transcrit dans le sien. Compté depuis le relâchement, B
        # dépasserait (0,45 + 0,4 > 0,6) : elle était jetée à tort.
        c = self._monte([(0.45, "première", None), (0.4, "seconde", None)])
        res = {}
        ta = threading.Thread(target=lambda: res.__setitem__("A", self._relache(c)))
        ta.start()
        while c._fin_next < 1:
            time.sleep(0.005)
        tb = threading.Thread(target=lambda: res.__setitem__("B", self._relache(c)))
        tb.start()
        ta.join(5)
        tb.join(5)
        self.assertEqual(res, {"A": "première", "B": "seconde"})
        self.assertEqual(self.copies, ["première", "seconde"])
        self.assertEqual((self.evts, self.toasts), ([], []))

    def test_le_superviseur_voit_une_vraie_panne(self):
        """Le battement s'arrête au-delà du budget : une transcription figée
        redevient visible pour le superviseur (et le chien de garde a parlé)."""
        c = self._monte([(self.BUDGET + 2.6, "trop tard", None)])
        c._fin_beat = 0.0
        t = threading.Thread(target=lambda: self._relache(c))
        t.start()
        time.sleep(self.BUDGET + 2.3)
        self.assertLess(c._fin_beat, time.time() - 2.0 + 0.3)
        self.assertEqual(self.evts, [E.FINALIZE_TIMEOUT])
        t.join(5)


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
            "os": os, "appstate": types.SimpleNamespace(
                model_dir=tmp.name, small_model_dir="/nope",
                cpu_threads=4, engine_load_params=None),
            "_resolve_model_dirs": lambda: ordre.append(("resolve",)),
            "_point_mlx_to_cache": lambda: ordre.append(("point",)),
            "_load_engine_async": lambda: None,
            "threading": types.SimpleNamespace(Thread=fil)})
        with mock.patch.dict(sys.modules, {"mlx_engine": mlx}):
            self.assertTrue(ns["_finish_model_download"]())
        self.assertEqual(ordre, [("resolve",), ("point",), ("reset", "modèle téléchargé")])
        mlx.available.assert_not_called()
        self.assertEqual(ns["appstate"].engine_load_params, (tmp.name, 4))
        fil.return_value.start.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
