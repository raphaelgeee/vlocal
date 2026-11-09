"""Constat 8 — le micro de réunion s'ouvre hors _meeting_lock, en temps borné.

Avant : rec.start() tournait sous _meeting_lock, sans délai. Un pilote audio
figé gelait l'appel et toute autre transition de réunion. Maintenant : délai
borné, état rendu proprement, micro refermé s'il s'ouvre après l'abandon, et
message fixe (jamais le texte de l'exception). Aucun micro réel n'est ouvert.
"""
import os
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api_meetings  # noqa: E402
import appstate  # noqa: E402
import recorder  # noqa: E402
from meetings import _meeting_lock, _meeting_state  # noqa: E402


class _FauxMicro:
    """Doublure de MeetingRecorder : start() peut se figer ou échouer."""
    delai = 0.0
    erreur = None
    instances = []

    def __init__(self, device=None):
        self.recording = False
        self.stopped = threading.Event()
        _FauxMicro.instances.append(self)

    def start(self):
        time.sleep(self.delai)
        if self.erreur:
            raise self.erreur
        self.recording = True
        return "/nonexistent/reunion.wav"

    def stop(self):
        self.recording = False
        self.stopped.set()
        return ("/nonexistent/reunion.wav", 0.0)

    def rms_recent(self):
        return 0.0


class TestOuvertureMicroReunion(unittest.TestCase):
    def setUp(self):
        _FauxMicro.delai, _FauxMicro.erreur, _FauxMicro.instances = 0.0, None, []
        self.toasts = []
        for p in (mock.patch.object(appstate, "controller", mock.Mock()),
                  mock.patch.object(appstate, "reunion_enabled", True),
                  mock.patch.object(appstate, "store", mock.Mock()),
                  mock.patch.object(recorder, "MeetingRecorder", _FauxMicro),
                  mock.patch.object(api_meetings, "MIC_OPEN_TIMEOUT_S", 0.3, create=True),
                  mock.patch.object(api_meetings, "_load_settings", return_value={}),
                  mock.patch.object(api_meetings, "_diarization_enabled", return_value=False),
                  mock.patch.object(api_meetings, "_toast",
                                    lambda m, kind="info": self.toasts.append(m)),
                  mock.patch.object(api_meetings, "_ui"),
                  mock.patch.object(api_meetings, "_begin_activity"),
                  mock.patch.object(api_meetings, "overlay"),
                  mock.patch("live_meeting.LiveMeetingTranscriber"),
                  mock.patch.dict(_meeting_state, {"recording": False, "recorder": None,
                                                   "id": None, "pipeline_busy": False})):
            p.start()
            self.addCleanup(p.stop)

    def _demarrer(self):
        return api_meetings.MeetingsApi().reunion_start()

    def test_micro_fige_delai_borne_et_verrou_libre(self):
        _FauxMicro.delai = 1.5
        libre = []

        def sonde():
            time.sleep(0.1)
            ok = _meeting_lock.acquire(timeout=0.1)
            libre.append(ok)
            if ok:
                _meeting_lock.release()

        threading.Thread(target=sonde).start()
        t0 = time.monotonic()
        r = self._demarrer()
        self.assertLess(time.monotonic() - t0, 1.0, "ouverture du micro non bornée")
        self.assertFalse(r["ok"])
        self.assertEqual(libre, [True], "_meeting_lock tenu pendant l'ouverture du micro")
        self.assertFalse(_meeting_state["recording"])
        self.assertIsNone(_meeting_state["recorder"])
        # le micro qui s'ouvre après l'abandon est refermé par son propre fil
        self.assertTrue(_FauxMicro.instances[0].stopped.wait(3.0))

    def test_echec_d_ouverture_message_fixe_et_etat_rendu(self):
        _FauxMicro.erreur = RuntimeError("PaErrorCode -9998 SECRET")
        r = self._demarrer()
        self.assertFalse(r["ok"])
        self.assertNotIn("SECRET", r["error"])
        self.assertFalse(any("SECRET" in t for t in self.toasts), self.toasts)
        self.assertFalse(_meeting_state["recording"])

    def test_ouverture_normale_inchangee(self):
        r = self._demarrer()
        self.assertTrue(r["ok"], r)
        self.assertIs(_meeting_state["recorder"], _FauxMicro.instances[0])
        self.assertTrue(_meeting_state["recording"])
        _meeting_state["recording"] = False   # arrête la boucle de niveau


if __name__ == "__main__":
    unittest.main()
