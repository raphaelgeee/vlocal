"""Constats 13 et 14 — téléchargement, chargement du moteur, rappels, Réglages.

- 13 : un seul téléchargement des modèles, un seul chargement du moteur, même
  sous deux demandes simultanées ;
- 14 : nouvelles tentatives bornées du chargement du moteur ; rebond du Dock
  confié au thread principal ; minuteurs de démarrage en fils daemon ;
  ouverture des Réglages sans evaluate_js synchrone.
Aucun moteur, aucun réseau, aucun fichier de l'utilisateur : tout est doublé.
"""
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

import api_settings  # noqa: E402
import appstate  # noqa: E402
import engine_runtime  # noqa: E402
import reminder_flow  # noqa: E402
import ui_bridge  # noqa: E402


def _en_parallele(fn, n=2):
    fils = [threading.Thread(target=fn) for _ in range(n)]
    for t in fils:
        t.start()
    for t in fils:
        t.join(10)


class TestTelechargementUnique(unittest.TestCase):
    def test_deux_clics_simultanes_un_seul_telechargement(self):
        def disque_lent(_):
            time.sleep(0.3)        # élargit la fenêtre test-puis-pose
            return mock.Mock(free=10 ** 12)

        with mock.patch("shutil.disk_usage", disque_lent), \
                mock.patch.object(api_settings.model_store, "download_models") as dl, \
                mock.patch.object(api_settings, "_finish_model_download", return_value=True), \
                mock.patch.object(api_settings, "_ui"):
            _en_parallele(api_settings.SettingsApi().download_models)
        self.assertEqual(dl.call_count, 1)


class _ChargementMoteur(unittest.TestCase):
    def setUp(self):
        for p in (mock.patch.object(appstate, "engine_load_params",
                                    (tempfile.gettempdir(), 2)),
                  mock.patch.object(appstate, "controller", None),
                  mock.patch.object(engine_runtime, "DictationController"),
                  mock.patch.object(engine_runtime, "_engine_after_load", create=True),
                  mock.patch.object(engine_runtime, "_EV", None),
                  mock.patch.object(engine_runtime, "ENGINE_LOAD_RETRY_DELAYS_S",
                                    (0.0, 0.0), create=True)):
            p.start()
            self.addCleanup(p.stop)
        self.toasts = []
        p = mock.patch.object(engine_runtime, "_toast",
                              lambda m, kind="info": self.toasts.append(m))
        p.start()
        self.addCleanup(p.stop)


class TestChargementMoteur(_ChargementMoteur):
    def test_deux_demandes_simultanees_un_seul_moteur(self):
        def lent(**_):
            time.sleep(0.3)
            return mock.Mock()

        with mock.patch.object(engine_runtime, "VlocalEngine", side_effect=lent) as ve:
            _en_parallele(engine_runtime._load_engine_async)
        self.assertEqual(ve.call_count, 1)

    def test_moteur_deja_charge_pas_de_second(self):
        with mock.patch.object(appstate, "controller", mock.Mock()), \
                mock.patch.object(engine_runtime, "VlocalEngine") as ve:
            engine_runtime._load_engine_async()
        ve.assert_not_called()

    def test_echec_passager_nouvelle_tentative(self):
        with mock.patch.object(engine_runtime, "VlocalEngine",
                               side_effect=[OSError("verrou"), mock.Mock()]) as ve:
            engine_runtime._load_engine_async()
        self.assertEqual(ve.call_count, 2)
        self.assertIsNotNone(appstate.controller)
        self.assertEqual(self.toasts, [])

    def test_echec_persistant_borne_et_message_clair(self):
        with mock.patch.object(engine_runtime, "VlocalEngine",
                               side_effect=OSError("SECRET")) as ve:
            engine_runtime._load_engine_async()
        self.assertEqual(ve.call_count, 3)
        self.assertIsNone(appstate.controller)
        self.assertEqual(len(self.toasts), 1)
        self.assertNotIn("SECRET", self.toasts[0])


class TestRebondDuDock(unittest.TestCase):
    def test_request_user_attention_confie_au_thread_principal(self):
        import AppKit
        import Foundation
        file = mock.Mock()
        nsapp = mock.Mock()
        with mock.patch.object(reminder_flow, "notifier"), \
                mock.patch.object(reminder_flow, "_ui"), \
                mock.patch.object(reminder_flow.sys, "platform", "darwin"), \
                mock.patch.object(Foundation, "NSOperationQueue") as q, \
                mock.patch.object(AppKit, "NSApplication", nsapp):
            q.mainQueue.return_value = file
            reminder_flow._fire_reminder("Appeler Paul")
            nsapp.sharedApplication.return_value.requestUserAttention_.assert_not_called()
            file.addOperationWithBlock_.assert_called_once()
            file.addOperationWithBlock_.call_args.args[0]()      # la file principale l'exécute
        nsapp.sharedApplication.return_value.requestUserAttention_.assert_called_once_with(1)


class TestMinuteursDaemon(unittest.TestCase):
    def test_attente_des_modeles_minuteur_daemon(self):
        crees = []

        class _Timer(threading.Timer):
            def start(self):
                crees.append(self)

        with mock.patch.object(engine_runtime, "_onboarding_done", return_value=False), \
                mock.patch.object(engine_runtime.threading, "Timer", _Timer):
            engine_runtime._post_startup_model_checks()
        self.assertEqual(len(crees), 1)
        self.assertTrue(crees[0].daemon)

    def test_minuteurs_de_demarrage_daemon(self):
        src = open(os.path.join(RACINE, "startup.py"), encoding="utf-8").read()
        self.assertNotRegex(src, r"Timer\([^()]*\)\.start\(\)")


class TestOuvertureReglages(unittest.TestCase):
    def test_aucun_evaluate_js_synchrone(self):
        import Foundation
        fen = mock.Mock()
        with mock.patch.object(appstate, "window", fen), \
                mock.patch.object(Foundation, "NSOperationQueue"), \
                mock.patch.object(ui_bridge, "_ui") as ui:
            ui_bridge._open_settings_modal_robust()
            time.sleep(0.6)       # l'ancien code relançait evaluate_js depuis un fil
        fen.evaluate_js.assert_not_called()
        self.assertTrue(any("activateView('reglages')" in c.args[0] for c in ui.call_args_list))


if __name__ == "__main__":
    unittest.main()
