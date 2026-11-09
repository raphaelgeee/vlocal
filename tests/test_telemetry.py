"""Télémétrie déclarée (v1.1.0) : contenu exact des envois et compteurs d'usage."""
import os
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app_sources  # noqa: E402
import storage  # noqa: E402
import telemetry  # noqa: E402

# Liste FERMÉE de ce que l'app a le droit d'envoyer. Toute addition doit être
# décidée, écrite dans le README et dans la page Confidentialité, puis ajoutée
# ici : ce test échoue tant que ce n'est pas fait.
ALLOWED_INSTALL_KEYS = {"install_id", "first_name", "last_name", "email",
                        "app_version", "os_version", "last_seen_at", "last_used_at",
                        "mac_model", "ui_lang", "hotkey", "engine"}
ALLOWED_USAGE_KEYS = {"install_id", "day", "dictations", "words", "seconds_saved",
                      "audio_seconds", "meetings", "meeting_words"}
# Les incidents ne voyagent qu'en compteurs : jamais le contexte d'un événement.
ALLOWED_INCIDENT_KEYS = {"day", "code", "count"}


class UsageCountersTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = storage.Store(os.path.join(self.tmp.name, "t.db"))

    def tearDown(self):
        self.store.close() if hasattr(self.store, "close") else None
        self.tmp.cleanup()

    def test_record_usage_accumulates_per_day(self):
        self.store.record_usage(100, day="2026-09-06")
        self.store.record_usage(50, day="2026-09-06")
        self.store.record_usage(10, day="2026-09-05")
        rows = self.store.usage_days("2026-09-05")
        self.assertEqual([r["day"] for r in rows], ["2026-09-05", "2026-09-06"])
        today = rows[1]
        self.assertEqual(today["dictations"], 2)
        self.assertEqual(today["words"], 150)
        self.assertAlmostEqual(today["seconds_saved"],
                               150 * storage.SECONDS_SAVED_PER_WORD, places=3)
        tot = self.store.usage_totals()
        self.assertEqual((tot["dictations"], tot["words"]), (3, 160))

    def test_seconds_saved_matches_dashboard_formula(self):
        # accueil : mots/40 - mots/150 minutes
        words = 1000
        expected_min = words / 40 - words / 150
        self.assertAlmostEqual(words * storage.SECONDS_SAVED_PER_WORD / 60, expected_min)

    def test_word_count(self):
        self.assertEqual(storage.Store.word_count("  Bonjour à  tous\nça va "), 5)
        self.assertEqual(storage.Store.word_count(""), 0)


class PayloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = storage.Store(os.path.join(self.tmp.name, "t.db"))
        self.settings = {"install_id": telemetry.new_install_id(),
                         "first_name": " Raphael ", "last_name": "Gee",
                         "email": "raphael@example.org",
                         "telemetry_enabled": True,
                         # champs qui ne doivent JAMAIS partir
                         "license_email": "x@y.z", "obsidian_vault": "/Users/x/vault",
                         "hotkey": "ctrl_cmd"}

    def tearDown(self):
        self.tmp.cleanup()

    def test_rows_contain_only_declared_fields(self):
        self.store.record_usage(20)
        install, usage, _ = telemetry.build_rows(self.settings, self.store, "1.1.0", "15.5")
        self.assertEqual(set(install), ALLOWED_INSTALL_KEYS)
        self.assertEqual(install["first_name"], "Raphael")
        self.assertEqual(len(usage), 1)
        self.assertEqual(set(usage[0]), ALLOWED_USAGE_KEYS)
        self.assertEqual(usage[0]["dictations"], 1)
        self.assertEqual(usage[0]["words"], 20)
        self.assertEqual(install["email"], "raphael@example.org")
        blob = repr((install, usage))
        for forbidden in ("x@y.z", "/Users/x/vault", "ctrl_cmd"):
            self.assertNotIn(forbidden, blob)

    def test_invalid_email_is_dropped_not_sent(self):
        for bad in ("pas-une-adresse", "a@b", "deux@@arobases.fr", "espace @x.fr", "x" * 250):
            s = dict(self.settings, email=bad)
            install, _, _ = telemetry.build_rows(s, self.store, "1.2.0", "15.5")
            self.assertEqual(install["email"], "", f"adresse refusée attendue : {bad!r}")
        self.assertTrue(telemetry.valid_email("Prenom.Nom+tag@sous.domaine.fr"))

    def test_last_used_at_is_sent_when_known(self):
        s = dict(self.settings, last_used_at=1_800_000_000)
        install, _, _ = telemetry.build_rows(s, self.store, "1.2.0", "15.5")
        self.assertTrue(install["last_used_at"].startswith("20"))
        install2, _, _ = telemetry.build_rows(self.settings, self.store, "1.2.0", "15.5")
        self.assertIsNone(install2["last_used_at"])

    def test_meeting_counters_travel(self):
        self.store.record_meeting(300, 1200.0)
        _, usage, _ = telemetry.build_rows(self.settings, self.store, "1.2.0", "15.5")
        self.assertEqual(usage[-1]["meetings"], 1)
        self.assertEqual(usage[-1]["meeting_words"], 300)

    def test_machine_profile_travels_and_stays_technical(self):
        prof = {"mac_model": "Apple M2 Pro", "ui_lang": "fr", "hotkey": "fn",
                "engine": "mlx", "serial": "C02XXXX"}  # champ inconnu : ignoré
        install, _, _ = telemetry.build_rows(self.settings, self.store, "1.3.0", "15.5",
                                             profile=prof)
        self.assertEqual(set(install), ALLOWED_INSTALL_KEYS)
        self.assertEqual(install["mac_model"], "Apple M2 Pro")
        self.assertEqual(install["engine"], "mlx")
        self.assertNotIn("C02XXXX", repr(install))

    def test_machine_profile_is_optional(self):
        install, _, _ = telemetry.build_rows(self.settings, self.store, "1.3.0", "15.5")
        self.assertEqual(install["mac_model"], "")
        self.assertEqual(install["engine"], "")

    def test_audio_seconds_travels(self):
        self.store.record_usage(20, audio_s=12.5)
        _, usage, _ = telemetry.build_rows(self.settings, self.store, "1.3.0", "15.5")
        self.assertEqual(set(usage[0]), ALLOWED_USAGE_KEYS)
        self.assertAlmostEqual(usage[0]["audio_seconds"], 12.5, places=1)

    def test_incidents_are_counters_only(self):
        log = os.path.join(self.tmp.name, "events.jsonl")
        today = time.strftime("%Y-%m-%d")
        with open(log, "w", encoding="utf-8") as f:
            f.write('{"ts": "%sT10:00:00", "code": "mic_busy", "ctx": "MacBook de Raphael"}\n' % today)
            f.write('{"ts": "%sT11:00:00", "code": "mic_busy", "ctx": "secret"}\n' % today)
            f.write('{"ts": "%sT11:30:00", "code": "gpu_fallback"}\n' % today)
            f.write('pas du json\n')
            f.write('{"ts": "2020-01-01T00:00:00", "code": "vieux"}\n')
        rows = telemetry.read_incidents(today, path=log)
        self.assertEqual(rows, [{"day": today, "code": "gpu_fallback", "count": 1},
                                {"day": today, "code": "mic_busy", "count": 2}])
        for r in rows:
            self.assertEqual(set(r), ALLOWED_INCIDENT_KEYS)
        self.assertNotIn("MacBook", repr(rows))

    def test_incidents_never_raise_without_log(self):
        self.assertEqual(telemetry.read_incidents("2026-01-01", path="/nope/nope.jsonl"), [])

    def test_usage_window_covers_a_month_not_a_year(self):
        """Assez large pour réparer un trou (Mac éteint une semaine), assez
        étroite pour ne jamais dépasser la borne de la fonction serveur."""
        recent = time.strftime("%Y-%m-%d", time.localtime(time.time() - 10 * 86400))
        ancient = time.strftime("%Y-%m-%d", time.localtime(time.time() - 400 * 86400))
        self.store.record_usage(5, day=ancient)
        self.store.record_usage(11, day=recent)
        self.store.record_usage(7)
        _, usage, _ = telemetry.build_rows(self.settings, self.store, "1.1.0", "15.5")
        self.assertEqual(sorted(u["words"] for u in usage), [7, 11])
        self.assertLessEqual(len(usage), 31)

    def test_disabled_or_undecided_sends_nothing(self):
        calls = []
        fake = lambda install, usage, inc: calls.append(install)
        for value in (None, False):
            s = dict(self.settings, telemetry_enabled=value)
            self.assertFalse(telemetry.sync_once(s, self.store, "1.1.0", "15", send=fake))
        self.assertEqual(calls, [])

    def test_enabled_sends_once_and_never_raises(self):
        calls = []
        fake = lambda install, usage, inc: calls.append((install["install_id"], len(usage)))
        self.store.record_usage(3)
        self.assertTrue(telemetry.sync_once(self.settings, self.store, "1.1.0", "15",
                                            send=fake))
        self.assertEqual(calls, [(self.settings["install_id"], 1)])

        def boom(install, usage, inc):
            raise OSError("offline")
        self.assertFalse(telemetry.sync_once(self.settings, self.store, "1.1.0", "15",
                                             send=boom))


class SchedulerTests(unittest.TestCase):
    """Le planificateur doit VRAIMENT transporter le profil machine jusqu'à
    l'envoi. Un `get_profile` non branché ne casse rien de visible : les colonnes
    restent simplement vides pour tout le monde, en silence."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = storage.Store(os.path.join(self.tmp.name, "t.db"))
        self.store.record_usage(4)

    def tearDown(self):
        self.tmp.cleanup()

    def test_profile_reaches_the_payload(self):
        vu = {}
        settings = {"install_id": telemetry.new_install_id(), "first_name": "A",
                    "last_name": "B", "telemetry_enabled": True}
        t = telemetry.Telemetry(lambda: settings, lambda p: settings.update(p or {}),
                                lambda: self.store, "1.3.0", "15.5",
                                get_profile=lambda: {"mac_model": "Apple M5", "ui_lang": "en",
                                                     "hotkey": "right_opt", "engine": "cpu"})
        original = telemetry.sync_once
        telemetry.sync_once = lambda *a, **k: vu.update(k["get_profile"]()) or True
        try:
            self.assertTrue(t.sync_now())
        finally:
            telemetry.sync_once = original
        self.assertEqual(vu, {"mac_model": "Apple M5", "ui_lang": "en",
                              "hotkey": "right_opt", "engine": "cpu"})

    def test_a_failing_profile_never_blocks_the_send(self):
        settings = {"install_id": telemetry.new_install_id(), "telemetry_enabled": True}
        def explose():
            raise RuntimeError("sysctl indisponible")
        envoyes = []
        self.assertTrue(telemetry.sync_once(settings, self.store, "1.3.0", "15.5",
                                            send=lambda i, u, inc: envoyes.append(i),
                                            get_profile=explose))
        self.assertEqual(envoyes[0]["engine"], "")
        self.assertEqual(envoyes[0]["mac_model"], "")

    def test_nothing_is_computed_before_consent(self):
        """v1.4.4 — ni profil machine ni lecture du moteur tant que l'utilisateur
        n'a pas dit oui : le profil n'est demandé qu'après le consentement."""
        appels = []
        def profil():
            appels.append(1)
            return {"engine": "mlx"}
        for valeur in (None, False):
            settings = {"install_id": telemetry.new_install_id(), "telemetry_enabled": valeur}
            t = telemetry.Telemetry(lambda: settings, lambda p: None, lambda: self.store,
                                    "1.4.4", "15.5", get_profile=profil)
            self.assertFalse(t.sync_now())
        self.assertEqual(appels, [])
        settings = {"install_id": telemetry.new_install_id(), "telemetry_enabled": True}
        envoyes = []
        self.assertTrue(telemetry.sync_once(settings, self.store, "1.4.4", "15.5",
                                            send=lambda i, u, inc: envoyes.append(i),
                                            get_profile=profil))
        self.assertEqual(appels, [1])
        self.assertEqual(envoyes[0]["engine"], "mlx")


def _faux_moteurs(backend, statut):
    """Instance moteur et module mlx_engine simulés selon le contrat livré par le
    lot GPU : last_backend() est une MÉTHODE de VlocalEngine, status() une
    fonction de mlx_engine. La sonde available() échoue si elle est appelée :
    la télémétrie ne doit que lire."""
    eng = types.SimpleNamespace(last_backend=mock.Mock(return_value=backend))
    mlx = types.ModuleType("mlx_engine")
    mlx.status = mock.Mock(return_value=statut)
    mlx.available = mock.Mock(side_effect=AssertionError("sonde GPU lancée par la télémétrie"))
    return eng, mlx


def _profil_comme_app(eng):
    """Même appel que app._telemetry_profile."""
    return telemetry.machine_profile("fr", "fn", eng.last_backend())


class EngineFieldTests(unittest.TestCase):
    """v1.4.4 — champ `engine` : moteur réel de la dernière dictée, jamais la sonde."""

    def _profil(self, backend, statut):
        eng, mlx = _faux_moteurs(backend, statut)
        with mock.patch.dict(sys.modules, {"mlx_engine": mlx}), \
                mock.patch.object(telemetry, "_mac_model", return_value="Apple M5 (Mac17,2)"):
            prof = _profil_comme_app(eng)
        mlx.available.assert_not_called()
        return prof, eng, mlx

    def test_profile_never_probes_the_gpu(self):
        for backend in ("mlx", "cpu", ""):
            for etat in ("ok", "suspended", "retry", "unsupported", "disabled", "unknown"):
                prof, eng, mlx = self._profil(backend, (etat, "raison"))
                eng.last_backend.assert_called_once_with()
                mlx.status.assert_called_once_with()
                self.assertIn(prof["engine"], ("mlx", "cpu", ""))
        self.assertEqual(prof, {"mac_model": "Apple M5 (Mac17,2)", "ui_lang": "fr",
                                "hotkey": "fn", "engine": ""})

    def test_engine_is_the_last_dictation_backend(self):
        for backend in ("mlx", "cpu"):
            for etat in ("ok", "suspended", "retry", "unknown"):
                prof, _, _ = self._profil(backend, (etat, ""))
                self.assertEqual(prof["engine"], backend)

    def test_without_dictation_the_gpu_verdict_is_read(self):
        attendu = {"ok": "mlx", "suspended": "cpu", "retry": "cpu", "unsupported": "cpu",
                   "disabled": "cpu", "unknown": ""}
        for etat, valeur in attendu.items():
            self.assertEqual(telemetry.engine_label("", (etat, "")), valeur, etat)
            prof, _, _ = self._profil("", (etat, ""))
            self.assertEqual(prof["engine"], valeur, etat)

    def test_engine_values_stay_in_the_server_vocabulary(self):
        for backend in ("mlx", "cpu", "", "metal", None):
            for etat in ("ok", "suspended", "retry", "unsupported", "disabled", "unknown", "x"):
                self.assertIn(telemetry.engine_label(backend, (etat, "")), ("mlx", "cpu", ""))

    def test_gpu_state_unreadable_leaves_the_field_empty(self):
        _, mlx = _faux_moteurs("", ("ok", ""))
        mlx.status.side_effect = RuntimeError("état illisible")
        with mock.patch.dict(sys.modules, {"mlx_engine": mlx}), \
                mock.patch.object(telemetry, "_mac_model", return_value=""):
            self.assertEqual(telemetry.machine_profile("en", "", "")["engine"], "")
        mlx.available.assert_not_called()

    def test_full_sync_never_probes_the_gpu(self):
        """De bout en bout : le planificateur, branché comme dans app.py, envoie
        le moteur de la dernière dictée sans jamais lancer la sonde."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = storage.Store(os.path.join(tmp.name, "t.db"))
        settings = {"install_id": telemetry.new_install_id(), "telemetry_enabled": True}
        eng, mlx = _faux_moteurs("cpu", ("ok", ""))
        envoyes = []
        def envoi(i, u, inc):
            envoyes.append(i)
        # Seul l'envoi réseau est remplacé ; sync_once et le planificateur sont les vrais.
        with mock.patch.dict(sys.modules, {"mlx_engine": mlx}), \
                mock.patch.object(telemetry, "sync_once",
                                  lambda *a, **k: _vrai_sync_once(*a, send=envoi, **k)):
            t = telemetry.Telemetry(lambda: settings, lambda p: None, lambda: store,
                                    "1.4.4", "15.5", get_profile=lambda: _profil_comme_app(eng))
            self.assertTrue(t.sync_now())
        mlx.available.assert_not_called()
        self.assertEqual(envoyes[0]["engine"], "cpu")

    def test_app_profile_reads_the_engine_instance(self):
        """Garde-fou : app.py ne passe plus par mlx_engine.available() pour le
        profil, et lit last_backend() sur l'instance moteur."""
        src = app_sources.texte_complet()
        corps = src[src.index("def _telemetry_profile"):]
        corps = corps[:corps.index("\ndef ", 1)]
        self.assertIn(".engine.last_backend()", corps)
        self.assertNotIn("available()", corps)
        self.assertNotIn("def _machine_profile", src)


_vrai_sync_once = telemetry.sync_once


class IncidentCodeTests(unittest.TestCase):
    """v1.4.4 — l'étape d'un incident voyage dans le code (« code:where »),
    seul format compatible avec la table `incidents` (install_id, day, code,
    count) et le nettoyage serveur ([a-zA-Z0-9_.:-], 40 caractères)."""

    SERVEUR_OK = __import__("re").compile(r"^[a-zA-Z0-9_.:-]{1,40}$")

    def test_where_is_suffixed(self):
        self.assertEqual(telemetry.incident_code(
            {"code": "mic_device_fail", "ctx": {"where": "start", "err": "PortAudio -9986"}}),
            "mic_device_fail:start")
        self.assertEqual(telemetry.incident_code({"code": "silence", "ctx": {"rms": 0.01}}),
                         "silence")
        self.assertEqual(telemetry.incident_code({"code": "gpu_timeout"}), "gpu_timeout")
        self.assertEqual(telemetry.incident_code({"ctx": {"where": "start"}}), "")

    def test_free_text_never_travels(self):
        for where in ("MacBook de Raphael", "AirPods Pro", "../etc", "x" * 40, "Start",
                      "unknown_step", 3, ["start"], {"a": 1}, None):
            code = telemetry.incident_code({"code": "mic_recovered", "ctx": {"where": where}})
            self.assertEqual(code, "mic_recovered")

    def test_every_known_step_fits_the_server(self):
        etapes = {"mic_device_fail": ("start", "start_retry", "fallback_builtin",
                                      "fallback_exhausted", "stream_dead", "start_lock_stuck"),
                  "mic_recovered": ("hard_reset", "builtin", "device"),
                  "diar_fallback": ("rediar_exception", "rediar_empty")}
        for code, wheres in etapes.items():
            for w in wheres:
                c = telemetry.incident_code({"code": code, "ctx": {"where": w}})
                self.assertEqual(c, f"{code}:{w}")
                self.assertRegex(c, self.SERVEUR_OK)

    def test_known_steps_are_a_closed_list(self):
        import errors
        self.assertIn("fallback_exhausted", errors.WHERE_CODES)
        for w in errors.WHERE_CODES:
            self.assertRegex(w, r"^[a-z0-9_]+$")

    def test_too_long_is_cut_cleanly(self):
        """Jamais d'étape tronquée ni de « : » final : le code seul part."""
        long_code = "c" * 30
        self.assertEqual(telemetry.incident_code(
            {"code": long_code, "ctx": {"where": "fallback_exhausted"}}), long_code)
        tres_long = "d" * 45
        self.assertEqual(telemetry.incident_code({"code": tres_long}), "d" * 40)
        c = telemetry.incident_code({"code": "x" * 22, "ctx": {"where": "fallback_exhausted"}})
        self.assertFalse(c.endswith(":"))
        self.assertLessEqual(len(c), 40)

    def test_read_incidents_payload_keeps_the_cause(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        log = os.path.join(tmp.name, "events.jsonl")
        today = time.strftime("%Y-%m-%d")
        lignes = [
            {"code": "mic_device_fail", "ctx": {"where": "fallback_exhausted",
                                                "err": "AirPods de Raphael", "steps": "start,start_retry"}},
            {"code": "mic_device_fail", "ctx": {"where": "stream_dead"}},
            {"code": "mic_device_fail", "ctx": {"where": "stream_dead"}},
            {"code": "mic_recovered", "level": "info", "ctx": {"where": "device", "device": "Micro USB"}},
            {"code": "transcribe_fail", "ctx": {"reason": "fail"}},
            ["pas", "un", "objet"],
        ]
        with open(log, "w", encoding="utf-8") as f:
            for d in lignes:
                if isinstance(d, dict):
                    d = dict(d, ts=today + "T09:00:00")
                f.write(__import__("json").dumps(d) + "\n")
        rows = telemetry.read_incidents(today, path=log)
        self.assertEqual(rows, [
            {"day": today, "code": "mic_device_fail:fallback_exhausted", "count": 1},
            {"day": today, "code": "mic_device_fail:stream_dead", "count": 2},
            {"day": today, "code": "mic_recovered:device", "count": 1},
            {"day": today, "code": "transcribe_fail", "count": 1},
        ])
        for r in rows:
            self.assertEqual(set(r), ALLOWED_INCIDENT_KEYS)
            self.assertRegex(r["code"], self.SERVEUR_OK)
        for prive in ("AirPods", "Micro USB", "start_retry"):
            self.assertNotIn(prive, repr(rows))
        # La charge utile réellement envoyée reprend ces lignes telles quelles.
        settings = {"install_id": telemetry.new_install_id(), "telemetry_enabled": True}
        store = storage.Store(os.path.join(tmp.name, "t.db"))
        with mock.patch.object(telemetry, "EVENTS_LOG", log):
            _, _, incidents = telemetry.build_rows(settings, store, "1.4.4", "15.5")
        self.assertEqual(incidents, rows)


if __name__ == "__main__":
    unittest.main()


class FauxPrefs:
    """Préférences en mémoire, ou en panne si `casse`."""
    def __init__(self, valeur=None, casse=False):
        self.d = {"install_id": valeur} if valeur else {}
        self.casse = casse
    def get(self, k):
        if self.casse:
            raise RuntimeError("préférences indisponibles")
        return self.d.get(k)
    def set(self, k, v):
        if self.casse:
            raise RuntimeError("préférences indisponibles")
        self.d[k] = v


class TestIdentifiantInstallationStable(unittest.TestCase):
    """v1.3.6 : une réinstallation ne fabrique plus une deuxième personne."""

    def _settings(self, initial):
        store = dict(initial)
        return store, (lambda: dict(store)), (lambda patch: store.update(patch))

    def test_settings_prime_et_copie_alignee(self):
        store, load, save = self._settings({"install_id": "aaa"})
        prefs = FauxPrefs("bbb")
        self.assertEqual(telemetry.ensure_install_id(load, save, prefs), "aaa")
        self.assertEqual(prefs.d["install_id"], "aaa")

    def test_dossier_vide_identifiant_retrouve(self):
        store, load, save = self._settings({})
        prefs = FauxPrefs("ccc")
        self.assertEqual(telemetry.ensure_install_id(load, save, prefs), "ccc")
        self.assertEqual(store["install_id"], "ccc")

    def test_premier_lancement_nouvel_uuid_copie(self):
        store, load, save = self._settings({})
        prefs = FauxPrefs()
        ident = telemetry.ensure_install_id(load, save, prefs)
        self.assertEqual(len(ident), 36)
        self.assertEqual(store["install_id"], ident)
        self.assertEqual(prefs.d["install_id"], ident)

    def test_preferences_en_panne_ne_bloquent_pas(self):
        store, load, save = self._settings({})
        prefs = FauxPrefs(casse=True)
        # MacPrefs absorbe ses erreurs ; un objet qui lève doit être toléré au même titre.
        try:
            ident = telemetry.ensure_install_id(load, save, prefs)
        except RuntimeError:
            self.fail("une panne des préférences a bloqué le démarrage")
        self.assertEqual(store["install_id"], ident)

    def test_mac_prefs_desactivees_ne_levent_jamais(self):
        # Sans suite (ou en banc d'essai), aucune écriture dans les préférences de la session.
        p = telemetry.MacPrefs(suite=None)
        p.set("essai_vlocal", "x")
        self.assertIsNone(p.get("essai_vlocal"))

    def test_mac_prefs_en_banc_d_essai_sont_muettes(self):
        import os
        avant = os.environ.get("VLOCAL_INSTANCE_SCOPE")
        os.environ["VLOCAL_INSTANCE_SCOPE"] = "home"
        try:
            self.assertIsNone(telemetry.MacPrefs().get("install_id"))
        finally:
            if avant is None:
                del os.environ["VLOCAL_INSTANCE_SCOPE"]
            else:
                os.environ["VLOCAL_INSTANCE_SCOPE"] = avant
