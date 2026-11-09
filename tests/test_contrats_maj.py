"""Lot propreté — contrats du lot mise à jour branchés dans l'app (1 à 8).

Toutes les fonctions d'updater et de model_store sont doublées : aucun réseau,
aucun fichier hors d'un dossier temporaire, aucune sortie de processus.
"""
import ast
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api_settings  # noqa: E402
import api_system  # noqa: E402
import app_sources  # noqa: E402
import appstate  # noqa: E402
import model_store  # noqa: E402
import startup  # noqa: E402
import updater  # noqa: E402


class _FilSynchrone:
    """threading.Thread qui exécute sa cible à start() (déroulé déterministe)."""

    def __init__(self, target=None, args=(), kwargs=None, **_):
        self._t, self._a, self._k = target, args, kwargs or {}

    def start(self):
        self._t(*self._a, **self._k)


class _Base(unittest.TestCase):
    def setUp(self):
        for nom, val in (("app_version", "1.4.3"), ("ui_lang", "fr")):
            p = mock.patch.object(appstate, nom, val)
            p.start()
            self.addCleanup(p.stop)
        self.ui = []
        p = mock.patch.object(api_system, "_ui", self.ui.append)
        p.start()
        self.addCleanup(p.stop)


class TestCheckUpdates(_Base):
    def _verifie(self, reponse, bloquee=False):
        corps = io.BytesIO(json.dumps(reponse).encode())
        corps.__enter__ = lambda *a: corps
        corps.__exit__ = lambda *a: False
        with mock.patch("urllib.request.urlopen", return_value=corps), \
                mock.patch.object(api_system.updater, "is_blocked", return_value=bloquee) as blk:
            return api_system.SystemApi().check_updates(), blk

    def test_empreinte_transmise_et_version_proposee(self):
        out, blk = self._verifie({"version": "1.5.0", "sha256": "ab" * 32, "url": "https://x/v.dmg"})
        self.assertEqual(out["sha256"], "ab" * 32)
        self.assertTrue(out["update"])
        blk.assert_called_once_with("1.5.0")

    def test_version_bloquee_jamais_reproposee(self):
        out, _ = self._verifie({"version": "1.5.0"}, bloquee=True)
        self.assertFalse(out["update"])
        self.assertIsNone(out["sha256"])

    def test_hors_ligne_sans_empreinte(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("hors ligne")):
            out = api_system.SystemApi().check_updates()
        self.assertTrue(out["offline"])
        self.assertIsNone(out["sha256"])


class TestPreparationEtInstallation(_Base):
    INFO = {"update": True, "latest": "1.5.0", "url": "https://x/v.dmg",
            "length": 10, "sha256": "cd" * 32}

    def setUp(self):
        super().setUp()
        for cible, val in ((api_system.threading, "Thread"),):
            p = mock.patch.object(cible, val, _FilSynchrone)
            p.start()
            self.addCleanup(p.stop)
        self.api = api_system.SystemApi()
        self.api.check_updates = lambda: dict(self.INFO)

    def test_telechargement_et_verification_complets(self):
        with mock.patch.object(api_system.updater, "download", return_value="/tmp/v.dmg") as dl, \
                mock.patch.object(api_system.updater, "verify_and_stage", return_value="/tmp/st") as vs:
            self.assertTrue(self.api.update_prepare()["ok"])
        self.assertEqual(dl.call_args.kwargs["sha256"], "cd" * 32)
        self.assertEqual(vs.call_args.kwargs["expected_version"], "1.5.0")
        self.assertEqual(vs.call_args.kwargs["current_version"], "1.4.3")
        self.assertIn("updateReady", self.ui[-1])

    def test_erreur_dite_dans_la_langue_de_l_interface(self):
        appstate.ui_lang = "en"
        err = updater.UpdateError("failed")
        with mock.patch.object(api_system.updater, "download", side_effect=err):
            self.api.update_prepare()
        self.assertIn(json.dumps(err.message("en")), self.ui[-1])
        self.assertNotIn(json.dumps(str(err)), self.ui[-1])

    def test_installation_avec_version_de_depart(self):
        with tempfile.TemporaryDirectory() as st, \
                mock.patch.object(api_system, "_update_staged", st), \
                mock.patch.object(api_system.threading, "Timer") as minuterie, \
                mock.patch.object(api_system.updater, "apply_update") as ap:
            self.assertEqual(self.api.update_apply(), {"ok": True})
        self.assertEqual(ap.call_args.kwargs["from_version"], "1.4.3")
        minuterie.return_value.start.assert_called_once_with()

    def test_echec_d_installation_dans_la_langue_de_l_interface(self):
        appstate.ui_lang = "en"
        err = updater.UpdateError("failed")
        with tempfile.TemporaryDirectory() as st, \
                mock.patch.object(api_system, "_update_staged", st), \
                mock.patch.object(api_system.updater, "apply_update", side_effect=err):
            self.assertEqual(self.api.update_apply(), {"ok": False, "error": err.message("en")})


class TestDemarrage(unittest.TestCase):
    def setUp(self):
        for nom, val in (("app_version", "1.4.3"), ("ui_lang", "fr")):
            p = mock.patch.object(appstate, nom, val)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(startup, "_rollback_notice", None)
        p.start()
        self.addCleanup(p.stop)

    def _tranche(self, rapport):
        with mock.patch.object(startup.updater, "cleanup_leftovers", return_value=rapport) as cl:
            fermer = startup.settle_pending_update()
        cl.assert_called_once_with("1.4.3")
        return fermer

    def test_retour_arriere_lance_ferme_l_app(self):
        self.assertTrue(self._tranche({"status": "rollback_started", "version": "1.5.0"}))

    def test_cas_ordinaires_continuent(self):
        for statut in ("idle", "confirming", "updated", "installing", "failed"):
            self.assertFalse(self._tranche({"status": statut, "version": "1.5.0"}), statut)
        self.assertIsNone(startup._rollback_notice)

    def test_retour_arriere_annonce_une_fois_au_chargement(self):
        self.assertFalse(self._tranche({"status": "rolled_back", "version": "1.5.0"}))
        toasts = []
        with mock.patch.object(startup.threading, "Thread", _FilSynchrone), \
                mock.patch.object(startup.updater, "confirm_startup") as conf, \
                mock.patch.object(startup, "_toast", lambda m, kind="info": toasts.append(m)):
            startup.on_main_window_loaded()
            startup.on_main_window_loaded()     # rechargement de la page : pas de doublon
        self.assertEqual(conf.call_count, 2)
        conf.assert_called_with("1.4.3")
        self.assertEqual(toasts, ["La version 1.5.0 n'a pas démarré correctement : "
                                  "Vlocal est revenu à la version précédente."])

    def test_message_anglais(self):
        appstate.ui_lang = "en"
        self.assertEqual(startup.rollback_message("1.5.0"),
                         "Version 1.5.0 did not start correctly: Vlocal went back "
                         "to the previous version.")

    def test_main_branche_le_demarrage(self):
        fichier, main = app_sources.definitions()["main"]
        appels = {ast.unparse(c.func) for c in ast.walk(main) if isinstance(c, ast.Call)}
        self.assertIn("settle_pending_update", appels)
        self.assertIn("on_main_window_loaded", ast.unparse(main))


class TestTelechargementDesModeles(unittest.TestCase):
    def setUp(self):
        # errors.log écrit dans le journal réel de l'utilisateur : doublé ici
        # (l'échec renvoyé par _api_safe y est journalisé).
        for p in (mock.patch("shutil.disk_usage", return_value=mock.Mock(free=10 ** 12)),
                  mock.patch("errors.log")):
            p.start()
            self.addCleanup(p.stop)

    def test_disque_plein_message_clair(self):
        with mock.patch.object(model_store, "download_models",
                               side_effect=model_store.DownloadError("no_space", "ENOSPC")):
            out = api_settings.SettingsApi().download_models()
        self.assertEqual(out, {"ok": False, "error": "Espace disque insuffisant : "
                                                     "libérez environ 3 Go puis réessayez."})
        self.assertFalse(api_settings._DL_LOCK.locked())   # garde relâchée

    def test_autre_echec_message_par_defaut(self):
        with mock.patch.object(model_store, "download_models",
                               side_effect=model_store.DownloadError("network", "reset")):
            out = api_settings.SettingsApi().download_models()
        self.assertFalse(out["ok"])
        self.assertIn("connexion", out["error"])
        self.assertFalse(api_settings._DL_LOCK.locked())   # garde relâchée


if __name__ == "__main__":
    unittest.main()
