"""Constat 7 — une réunion interrompue se récupère.

- au démarrage, les réunions restées dans un statut transitoire passent en
  « error » (récupérable) et l'en-tête de leur WAV est réparé ;
- l'interface propose « Réessayer » (retry_meeting) et traduit les statuts ;
- l'installation d'une mise à jour est refusée pendant une réunion ;
- l'enregistreur rend l'en-tête et les trames durables (fsync) périodiquement.
Base, WAV et dossiers : tous temporaires.
"""
import os
import shutil
import struct
import sys
import tempfile
import threading
import unittest
import wave
from unittest import mock

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

import api_system  # noqa: E402
import appstate  # noqa: E402
import meetings  # noqa: E402
import recorder  # noqa: E402
import startup  # noqa: E402
from storage import Store  # noqa: E402


def _wav_interrompu(path, n_frames):
    """WAV dont l'en-tête annonce 0 trame mais qui en contient n_frames
    (fichier laissé par un arrêt brutal sans clôture)."""
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"")
    with open(path, "r+b") as f:
        f.seek(4)
        f.write(struct.pack("<L", 36))
        f.seek(40)
        f.write(struct.pack("<L", 0))
        f.seek(0, 2)
        f.write(b"\x01\x00" * n_frames)


class _Temp(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="vlocal_test_reunion_")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)


class TestRecuperationAuDemarrage(_Temp):
    def setUp(self):
        super().setUp()
        self.store = Store(os.path.join(self.dir, "vlocal.db"))
        self.addCleanup(self.store.close)
        p = mock.patch.object(appstate, "store", self.store)
        p.start()
        self.addCleanup(p.stop)

    def test_statuts_transitoires_passent_en_erreur(self):
        ids = {st: self.store.add_meeting(st, os.path.join(self.dir, st + ".wav"), st)
               for st in ("recording", "transcribing", "formatting", "ready", "error")}
        startup._recover_interrupted_meetings()
        statut = {st: self.store.get_meeting(i)["status"] for st, i in ids.items()}
        self.assertEqual(statut, {"recording": "error", "transcribing": "error",
                                  "formatting": "error", "ready": "ready",
                                  "error": "error"})

    def test_en_tete_du_wav_interrompu_repare(self):
        wav = os.path.join(self.dir, "reunion.wav")
        _wav_interrompu(wav, 16000 * 3)
        self.store.add_meeting("r", wav, "recording")
        startup._recover_interrupted_meetings()
        with wave.open(wav, "rb") as w:
            self.assertEqual(w.getnframes(), 16000 * 3)

    def test_recuperation_avant_le_chargement_du_moteur(self):
        src = open(os.path.join(RACINE, "startup.py"), encoding="utf-8").read()
        corps = src[src.index("def after_start():"):]
        self.assertLess(corps.index("_recover_interrupted_meetings()"),
                        corps.index("_load_engine_async"))


class TestRepairWavHeader(_Temp):
    def test_repare_puis_idempotent(self):
        wav = os.path.join(self.dir, "a.wav")
        _wav_interrompu(wav, 1234)
        self.assertTrue(recorder.repair_wav_header(wav))
        self.assertFalse(recorder.repair_wav_header(wav))
        with wave.open(wav, "rb") as w:
            self.assertEqual(w.getnframes(), 1234)

    def test_fichier_non_wav_intact(self):
        p = os.path.join(self.dir, "x.wav")
        with open(p, "wb") as f:
            f.write(b"pas un wav" * 10)
        self.assertFalse(recorder.repair_wav_header(p))
        self.assertEqual(open(p, "rb").read(), b"pas un wav" * 10)


class TestEnregistreurDurable(_Temp):
    def test_en_tete_et_trames_rendus_durables_pendant_l_enregistrement(self):
        path = os.path.join(self.dir, "rec.wav")
        rec = recorder.MeetingRecorder()
        wav = wave.open(path, "wb")
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        syncs = []
        lu = []

        def _fsync(fd):
            # Lu par un AUTRE descripteur au moment du fsync, comme le ferait
            # une relecture après un arrêt brutal.
            with wave.open(path, "rb") as r:
                lu.append(r.getnframes())
            syncs.append(fd)

        for _ in range(3):
            rec._q.put(b"\x02\x00" * 800)
        with mock.patch.object(recorder, "HEADER_REFRESH_SEC", 0.0), \
                mock.patch.object(recorder.os, "fsync", side_effect=_fsync):
            t = threading.Thread(target=rec._writer_loop, args=(wav,))
            t.start()
            while not rec._q.empty():
                threading.Event().wait(0.05)
            threading.Event().wait(0.4)
            rec._writer_stop.set()
            t.join(5)
        self.assertTrue(syncs, "aucun fsync pendant l'enregistrement")
        self.assertEqual(lu[-1], 2400)


class TestMiseAJourRefuseePendantUneReunion(_Temp):
    def _apply(self, **etat):
        with mock.patch.dict(meetings._meeting_state, etat), \
                mock.patch.object(api_system, "_update_staged", self.dir), \
                mock.patch.object(api_system.updater, "apply_update") as ap, \
                mock.patch.object(api_system.threading, "Timer") as tm:
            r = api_system.SystemApi().update_apply()
        return r, ap, tm

    def test_refus_pendant_l_enregistrement(self):
        r, ap, tm = self._apply(recording=True, pipeline_busy=False)
        self.assertFalse(r["ok"])
        self.assertIn("réunion", r["error"])
        ap.assert_not_called()
        tm.assert_not_called()

    def test_refus_pendant_le_traitement(self):
        r, ap, _ = self._apply(recording=False, pipeline_busy=True)
        self.assertFalse(r["ok"])
        ap.assert_not_called()

    def test_acceptee_hors_reunion(self):
        r, ap, _ = self._apply(recording=False, pipeline_busy=False)
        self.assertTrue(r["ok"])
        ap.assert_called_once()


class TestInterface(unittest.TestCase):
    def setUp(self):
        self.html = open(os.path.join(RACINE, "vlocal-interface.html"), encoding="utf-8").read()

    def test_bouton_reessayer_appelle_retry_meeting(self):
        self.assertIn("call('retry_meeting'", self.html)
        self.assertIn("m.status==='error'", self.html)

    def test_statuts_traduits_fr_en(self):
        for st in ("recording", "transcribing", "formatting", "error"):
            self.assertEqual(self.html.count(f'"label.status.{st}":'), 2, st)

    def test_refus_de_mise_a_jour_affiche(self):
        bloc = self.html[self.html.index("call('update_apply'"):][:600]
        self.assertIn("showToast", bloc)


class TestReessaiDepuisLInterface(unittest.TestCase):
    def test_identifiant_texte_de_la_page_accepte(self):
        import api_meetings
        store = mock.Mock()
        store.get_meeting.return_value = None
        with mock.patch.object(appstate, "controller", mock.Mock()), \
                mock.patch.object(appstate, "store", store):
            r = api_meetings.MeetingsApi().retry_meeting("12", None)
        store.get_meeting.assert_called_once_with(12)
        self.assertFalse(r["ok"])


if __name__ == "__main__":
    unittest.main()


class TestReessaiSansWav(_Temp):
    """« Réessayer » n'apparaît que si le WAV de la réunion en erreur existe."""

    def _meeting(self, wav):
        return {"id": 7, "status": "error", "wav_path": wav, "titre": "R"}

    def _detail(self, row):
        from api_meetings import MeetingsApi
        store = mock.Mock()
        store.get_meeting.return_value = dict(row)
        with mock.patch.object(appstate, "store", store):
            return MeetingsApi().get_meeting(7)

    def test_wav_present_retry_offered(self):
        wav = os.path.join(self.dir, "r.wav")
        _wav_interrompu(wav, 10)
        self.assertTrue(self._detail(self._meeting(wav))["can_retry"])

    def test_wav_absent_retry_hidden_and_api_says_why(self):
        from api_meetings import MeetingsApi
        row = self._meeting(os.path.join(self.dir, "disparu.wav"))
        self.assertFalse(self._detail(row)["can_retry"])
        store = mock.Mock()
        store.get_meeting.return_value = dict(row)
        with mock.patch.object(appstate, "store", store), \
                mock.patch.object(appstate, "controller", object()), \
                mock.patch.object(appstate, "ui_lang", "en"):
            r = MeetingsApi().retry_meeting(7)
        self.assertEqual(r["ok"], False)
        self.assertIn("no longer available", r["error"])

    def test_interface_hides_retry_without_wav(self):
        html = open(os.path.join(RACINE, "vlocal-interface.html"), encoding="utf-8").read()
        self.assertIn("if(m.status==='error' && m.can_retry)", html)
