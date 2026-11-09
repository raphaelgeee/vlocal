"""API du tableau de bord : dictée, mode, historique, rappels.

Méthodes appelées par le JavaScript (window.pywebview.api.*) : démarrer,
arrêter ou annuler une dictée depuis la fenêtre, choisir le mode (dictée ou
réunion), relire et vider l'historique, gérer les rappels.
"""
import os
import time

import appstate
import errors as _EV
import notifier
import overlay
import permissions
from dictation import copy_to_clipboard
from engine_runtime import _engine_ready
from reminder_flow import _cancel_reminder_timer, _schedule_reminder
from settings import _current_mode, _load_settings, _save_settings, _set_mode
from ui_bridge import (
    _api_safe,
    _cancel_overlay_hide,
    _refresh_lists_ui,
    _refresh_menubar,
    _toast,
    _ui,
)


class DictationApi:
    """Dictée depuis la fenêtre, mode, historique et rappels."""

    @_api_safe(default=False)
    def start_dictation(self):
        if appstate.controller is None:
            # v2 — moteur encore en préparation (démarrage différé).
            _toast("Le moteur de transcription finit de se préparer, un "
                   "instant…", kind="info")
            return False
        # v3.2.8 — micro EXPLICITEMENT refusé : message clair + on ne lance pas une
        # dictée silencieuse (le bouton Dicter n'affiche pas l'overlay -> échec muet
        # sinon). Cas "indéterminé" (status 0) : on laisse le prompt micro NATIF.
        try:
            if permissions.mic_status() == 2:
                if _EV: _EV.log(_EV.E.MIC_DENIED)
                _toast("Micro refusé. Autorisez Vlocal dans Réglages système "
                       "> Confidentialité > Microphone.", kind="error")
                return False
        except Exception:
            pass
        return bool(appstate.controller.start())

    def engine_ready(self):
        """v2 — True si le moteur Whisper est chargé et prêt."""
        return appstate.controller is not None and _engine_ready.is_set()

    @_api_safe(default=False)
    def copy_text(self, text):
        """v2 — Copie fiable via le système (pbcopy/clipboard), au lieu de
        navigator.clipboard qui échoue parfois silencieusement dans WKWebView."""
        return copy_to_clipboard(text or "")

    @_api_safe(default="")
    def stop_dictation(self):
        # Renvoie le texte BRUT final à l'UI (qui le révèle). La copie presse-papier
        # est déjà faite dans le contrôleur. Aucun post-traitement LLM ici (garde-fou).
        # _api_safe garantit qu'un échec rend "" plutôt qu'une promesse rejetée
        # (sinon l'UI resterait bloquée sur "Transcription en cours").
        if appstate.controller is None:
            return ""
        return appstate.controller.stop_and_finalize()

    @_api_safe(default=False)
    def cancel_dictation(self):
        """v2 — Annule la dictée en cours sans transcrire (touche Échap / bouton
        Annuler). Jette l'audio, instantané. True si une dictée a été annulée."""
        if appstate.controller is None:
            return False
        cancelled = appstate.controller.cancel()
        if cancelled:
            _ui("dictationCancelled()")
            _cancel_overlay_hide()
            overlay.reset()
        return cancelled

    def get_mode(self):
        """v11 — Retourne le mode actif (DICTEE/REUNION)."""
        return _current_mode()

    def set_mode(self, mode):
        """v11 — Change le mode actif. Persisté entre sessions. v3.3 — ne persiste
        PAS REUNION si le mode Réunion est désactivé (sinon réouverture en état
        incohérent : UI réunion mais bouton Enregistrer qui refuse)."""
        if (mode or "").upper() == "REUNION" and not appstate.reunion_enabled:
            _toast("Le mode Réunion n'est pas activé. Activez-le dans "
                   "Réglages.", kind="info")
            new = _set_mode("DICTEE")
        else:
            new = _set_mode(mode)
        _refresh_menubar()
        return new

    def is_reunion_enabled(self):
        return appstate.reunion_enabled

    # ------------------------ v18.4 : Historique + Stockage -------------------
    @_api_safe(default=[])
    def get_history(self, limit=200):
        """v18.4 — Timeline unifiée (réunions, dictées, notes, rappels) triée du
        plus récent au plus ancien. Chaque entrée : {type,id,titre,apercu,ts}."""
        if appstate.store is None:
            return []
        items = []
        try:
            for m in appstate.store.list_meetings(limit=limit):
                txt = (m.get("transcription_structuree")
                       or m.get("transcription_brute") or "")
                items.append({"type": "reunion", "id": m.get("id"),
                              "titre": m.get("titre") or "Réunion",
                              "apercu": (txt or "")[:140],
                              "ts": m.get("created_at") or 0})
        except Exception:
            pass
        try:
            for d in appstate.store.list_dictations(limit=limit):
                _c = d.get("content") or ""
                items.append({"type": "dictee", "id": d.get("id"),
                              "titre": "Dictée",
                              "apercu": _c[:140],
                              "full": _c,        # v29.6 — copie directe au clic
                              "ts": d.get("created_at") or 0})
        except Exception:
            pass
        try:
            for t in appstate.store.list_tasks(limit=limit):
                items.append({"type": "rappel", "id": t.get("id"),
                              "titre": t.get("titre") or "Rappel",
                              "apercu": (t.get("echeance") or ""),
                              "ts": t.get("created_at") or 0})
        except Exception:
            pass
        items.sort(key=lambda x: x.get("ts") or 0, reverse=True)
        return items[:limit]

    @_api_safe(default=False)
    def set_history_scope(self, scope):
        """v18.4 — Politique de sauvegarde : 'all' (dictées + réunions),
        'meetings' (réunions seulement) ou 'none'. Indiqué clairement à l'UI."""
        scope = scope if scope in ("all", "meetings", "none") else "all"
        _save_settings({"history_scope": scope})
        return True

    @_api_safe(default=0)
    def clear_history(self, scope):
        """v18.4 — Purge l'historique. scope : 'dictees', 'reunions' ou 'tout'.
        Renvoie le nombre d'éléments supprimés. Libère le stockage."""
        if appstate.store is None:
            return 0
        n = 0
        if scope in ("dictees", "tout"):
            n += appstate.store.clear_dictations()
        if scope in ("reunions", "tout"):
            n += appstate.store.clear_meetings(delete_wav=True)
        _refresh_lists_ui()
        return n

    @_api_safe(default={})
    def get_storage_info(self):
        """v18.4 — Espace occupé localement, présenté clairement à l'utilisateur :
        base de données + fichiers audio des réunions (les plus lourds)."""
        info = {"db_mo": 0.0, "audio_mo": 0.0, "total_mo": 0.0,
                "n_reunions": 0, "n_dictees": 0, "audio_files": 0,
                "stats_since": int(_load_settings().get("stats_since", 0) or 0)}
        try:
            if appstate.store is not None and os.path.exists(appstate.store.path):
                info["db_mo"] = round(os.path.getsize(appstate.store.path) / 1e6, 2)
        except Exception:
            pass
        try:
            if appstate.store is not None:
                # v20 — comptages/chemins LÉGERS : plus aucune transcription ni
                # JSON par mot matérialisés juste pour compter (RAM transitoire).
                info["n_reunions"] = appstate.store.count_meetings()
                info["n_dictees"] = appstate.store.count_dictations()
                # v18.5 — on somme les VRAIS fichiers audio référencés par les
                # réunions (wav_path), où qu'ils soient, plutôt qu'un dossier fixe.
                tot = 0
                n = 0
                for _mid, wp in appstate.store.list_meeting_wavs():
                    try:
                        if wp and os.path.exists(wp):
                            tot += os.path.getsize(wp)
                            n += 1
                    except Exception:
                        pass
                info["audio_mo"] = round(tot / 1e6, 2)
                info["audio_files"] = n
        except Exception:
            pass
        info["total_mo"] = round(info["db_mo"] + info["audio_mo"], 2)
        return info

    # v15 — API « Puissance machine » SUPPRIMÉE (plus de tier ni de SLM).

    # ---------- v6 : API pour la persistance (consommée par l'UI) ----------
    @_api_safe(default=list)
    def list_tasks(self):
        # v9 TOP-5 #5 — on relève la limite à 50, l'UI scroll
        return appstate.store.list_tasks(limit=50) if appstate.store else []

    @_api_safe(default=False)
    def rename_task(self, task_id, new_title):
        """v9 TOP-5 #4 — édition inline du titre d'une tâche."""
        if appstate.store is None or not new_title:
            return False
        ok = appstate.store.rename_task(int(task_id), str(new_title).strip()[:200])
        _refresh_menubar()
        return ok

    @_api_safe(default=None)
    def toggle_task(self, task_id):
        if appstate.store is None:
            return None
        new = appstate.store.toggle_task(int(task_id))
        # v20 — tâche cochée -> son timer ne sonnera pas ; re-décochée avant
        # l'échéance -> ré-armement.
        try:
            if new == "done":
                _cancel_reminder_timer(int(task_id))
            elif new == "open":
                for t in appstate.store.list_tasks(limit=500, only_open=True):
                    if t.get("id") == int(task_id):
                        ts = notifier.parse_iso(t.get("echeance_iso") or "")
                        if ts and ts > time.time():
                            _schedule_reminder(ts, (t.get("titre") or "")[:120],
                                               task_id=int(task_id))
                        break
        except Exception:
            pass
        _refresh_menubar()
        return new

    @_api_safe(default=False)
    def delete_task(self, task_id):
        if appstate.store is None:
            return False
        appstate.store.delete_task(int(task_id))
        _cancel_reminder_timer(int(task_id))   # v20 — ne sonne plus après suppression
        _refresh_menubar()
        return True
