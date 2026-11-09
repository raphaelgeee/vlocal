"""API du tableau de bord : réglages, glossaire, langue, modèles, diagnostic.

Méthodes appelées par le JavaScript (window.pywebview.api.*) : glossaire et
raccourcis vocaux, réglages JSON, langue de l'interface, téléchargement des
modèles au premier lancement, diagnostic envoyé au support, coffre Obsidian,
statistiques d'usage, consentement à la télémétrie.
"""
import json
import os
import threading

import applog
import appstate
import errors as _EV
import model_store
import obsidian
import overlay
import telemetry
from engine_runtime import _finish_model_download, _refresh_whisper_bias
from hotkeys import _rearm_global_hotkey
from lifecycle import _auto_diag_enabled
from settings import (
    _load_glossary_text,
    _load_settings,
    _save_glossary_text,
    _save_settings,
)
from supabase_config import auth_headers, edge_url
from texts import _te
from ui_bridge import _api_safe, _open_settings_modal_robust, _refresh_menubar, _ui

log = applog.get("api_settings")

_dl_in_flight = False       # v3.3 (N2) — garde anti-double téléchargement modèles


class SettingsApi:
    """Réglages, glossaire, langue, modèles, diagnostic, Obsidian et télémétrie."""

    # ------------------------ v13 : Glossaire personnel ----------------------
    # Suggestions proposées à la 1ʳᵉ ouverture (l'utilisateur coche celles
    # qu'il garde, rien n'est imposé). Inspiré du brief v13.
    GLOSSARY_SUGGESTIONS = [
        {"incorrect": "Captive",       "correct": "Captiv",     "notes": "Plateforme produit"},
        {"incorrect": "Néolife",       "correct": "Neolife",    "notes": "Client récurrent"},
        {"incorrect": "Oréégami",      "correct": "Oreegami",   "notes": "Client bootcamp marketing"},
        {"incorrect": "DoWino",        "correct": "DOWiNO",     "notes": "Casse exacte"},
        {"incorrect": "Made in IA",    "correct": "Made in AI", "notes": "Nom société (AI en anglais)"},
        {"incorrect": "Padcrabé",      "correct": "Putcrabey",  "notes": "Nom propre — interlocuteur"},
        {"incorrect": "Anthropique",   "correct": "Anthropic",  "notes": "Société Claude (anglais)"},
        {"incorrect": "SuperBase",     "correct": "Supabase",   "notes": "BaaS"},
        {"incorrect": "à Pifi",        "correct": "Apify",      "notes": "Scraping API"},
        {"incorrect": "Full on Reach", "correct": "FullEnrich", "notes": "Enrichissement B2B"},
        {"incorrect": "NOLIF",         "correct": "Neolife",    "notes": "Erreur Whisper observée"},
        {"incorrect": "Hopco",         "correct": "OPCO",       "notes": "Acronyme formation pro"},
        {"incorrect": "BGOM 3",        "correct": "BGE-M3",     "notes": "Modèle embedding"},
        {"incorrect": "Qdrand",        "correct": "Qdrant",     "notes": "Vector DB"},
        {"incorrect": "H Saint",       "correct": "H100",       "notes": "GPU Nvidia"},
        # v2 — Stack IA/dev courant : biaise le moteur dès le départ.
        {"incorrect": "Fulenrich",     "correct": "FullEnrich", "notes": "Enrichissement B2B"},
        {"incorrect": "Haïku",         "correct": "Haiku",      "notes": "Modèle Claude Haiku"},
        {"incorrect": "Anthropique",   "correct": "Anthropic",  "notes": "Société Claude"},
        {"incorrect": "Moku",          "correct": "mockup",     "notes": "Maquette UI"},
        {"incorrect": "Linked In",     "correct": "LinkedIn",   "notes": "Réseau pro"},
        {"incorrect": "prongs",        "correct": "prompts",    "notes": "Prompts LLM"},
        {"incorrect": "I C P",         "correct": "ICP",        "notes": "Ideal Customer Profile"},
        {"incorrect": "Webhook",       "correct": "webhook",    "notes": "Intégration"},
    ]

    @_api_safe(default=list)
    def list_glossary(self):
        return appstate.store.list_glossary() if appstate.store else []

    @_api_safe(default=list)
    def list_glossary_suggestions(self):
        """Suggestions non encore présentes dans la base (case insensitive)."""
        if appstate.store is None:
            return list(self.GLOSSARY_SUGGESTIONS)
        existing = {
            (e.get("incorrect") or "").strip().lower()
            for e in appstate.store.list_glossary()
        }
        return [
            s for s in self.GLOSSARY_SUGGESTIONS
            if s["incorrect"].strip().lower() not in existing
        ]

    @_api_safe(default=None)
    def add_glossary_entry(self, incorrect, correct, notes=""):
        if appstate.store is None:
            return None
        new_id = appstate.store.add_glossary_entry(incorrect, correct, notes or "")
        _refresh_whisper_bias()   # v15 — réinjecte le biais lexical (hotwords)
        return new_id

    @_api_safe(default=False)
    def delete_glossary_entry(self, entry_id):
        if appstate.store is None:
            return False
        appstate.store.delete_glossary_entry(int(entry_id))
        _refresh_whisper_bias()
        return True

    # ------------------------ Vlocal 2 : raccourcis vocaux -------------------
    @_api_safe(default=list)
    def list_snippets(self):
        return appstate.store.list_snippets() if appstate.store else []

    @_api_safe(default=None)
    def add_snippet(self, trigger, expansion):
        if appstate.store is None:
            return None
        return appstate.store.add_snippet(trigger, expansion)

    @_api_safe(default=False)
    def delete_snippet(self, snippet_id):
        if appstate.store is None:
            return False
        appstate.store.delete_snippet(int(snippet_id))
        return True

    @_api_safe(default=0)
    def import_glossary_suggestions(self, entries):
        """v13 — Import en lot des suggestions cochées par l'utilisateur.
        entries : liste de dicts {incorrect, correct, notes?}."""
        if appstate.store is None or not entries:
            return 0
        n = 0
        for e in entries:
            try:
                if appstate.store.add_glossary_entry(
                    e.get("incorrect", ""), e.get("correct", ""), e.get("notes", "")
                ):
                    n += 1
            except Exception:
                pass
        _refresh_whisper_bias()
        return n

    @_api_safe(default=False)
    def set_lang(self, lang):
        """v3.3 — fixe la langue de l'UI backend (fr/en) : messages côté Python
        (toasts, overlay, payloads d'erreur) + overlay de dictée. Poussé par le
        dashboard au démarrage et à chaque changement (il garde sa propre langue
        en localStorage ; ici on aligne le backend)."""
        appstate.ui_lang = "en" if str(lang).lower().startswith("en") else "fr"
        try:
            overlay.set_lang(appstate.ui_lang)
        except Exception:
            pass
        try:
            _refresh_menubar()   # relabel le menu NSStatusItem (Dicter/Ouvrir/Quitter…)
        except Exception:
            pass
        return True

    @_api_safe(default=False)
    def models_need_download(self):
        """v3.3 — True si les modèles Whisper doivent être téléchargés (app thin, 1er run)."""
        return bool(model_store.needs_download())

    @_api_safe(default=lambda: {"ok": False, "error": "Téléchargement des modèles échoué. Vérifiez votre connexion et réessayez."})
    def download_models(self):
        """v3.3 — Télécharge les modèles depuis R2 (bloquant ; progression poussée à
        l'UI via setModelProgress 0..1), puis lance le moteur. {ok:True} ou {ok:False,error}.
        Idempotent + reprise + 3 réessais par fichier (cf. model_store.download_models)."""
        global _dl_in_flight
        if _dl_in_flight:
            return {"ok": False, "error": "Téléchargement déjà en cours."}
        try:   # v3.3 — espace disque suffisant pour ~2,8 Go (sinon faux diagnostic 'connexion')
            import shutil as _sh
            if _sh.disk_usage(os.path.expanduser("~")).free < 3_500_000_000:
                return {"ok": False, "error": "Espace disque insuffisant : libérez environ 3 Go puis réessayez."}
        except Exception:
            pass
        def _prog(done, total, label):
            try:
                _ui("if(typeof setModelProgress==='function') setModelProgress(%.4f);"
                    % (float(done) / total if total else 0.0))
            except Exception:
                pass
        _dl_in_flight = True
        try:
            try:
                model_store.download_models(progress=_prog)
            except model_store.DownloadError as e:
                # Disque plein pendant le téléchargement : le dire clairement plutôt
                # que le message générique « vérifiez votre connexion ».
                if e.code == "no_space":
                    return {"ok": False, "error": "Espace disque insuffisant : "
                            "libérez environ 3 Go puis réessayez."}
                raise   # autre échec définitif : _api_safe renvoie le message par défaut
            if not _finish_model_download():
                # S1 — DL « réussi » mais moteur introuvable : NE PAS débloquer en silence,
                # remonter une erreur -> l'écran préparation montre Réessayer.
                return {"ok": False, "error": "Modèles téléchargés mais moteur introuvable. Réessayez."}
            return {"ok": True}
        finally:
            _dl_in_flight = False

    @_api_safe(default=lambda: {})
    def get_diagnostics(self):
        """v3.3 — TOUR DE CONTRÔLE : état complet pour le diagnostic (à copier/envoyer
        au support). 100% local : AUCUN texte transcrit (events.jsonl exclut déjà
        'text'), aucune donnée personnelle, rien n'est envoyé automatiquement."""
        import platform as _pf
        d = {"version": appstate.app_version, "lang": appstate.ui_lang}
        try:
            d["engine_ready"] = bool(self.engine_ready())
        except Exception:
            d["engine_ready"] = False
        try:
            import mlx_engine as _mlx
            d["gpu_mlx"] = bool(_mlx.available())
        except Exception:
            d["gpu_mlx"] = False
        try:
            d["models_need_download"] = bool(model_store.needs_download())
            d["models_dir"] = model_store.models_dir()
        except Exception:
            pass
        try:
            d["macos"] = _pf.mac_ver()[0]
            d["arch"] = _pf.machine()
        except Exception:
            pass
        try:
            import psutil as _ps
            d["ram_gb"] = round(_ps.virtual_memory().total / 2 ** 30, 1)
        except Exception:
            pass
        try:
            if _EV:
                d["errors_summary"] = _EV.summary()
                d["recent_events"] = _EV.recent(40)
        except Exception:
            pass
        # v1.0.11 — flag dev : pilote l'affichage du détail technique côté UI (les
        # utilisateurs ne voient que le formulaire d'envoi). N'affecte PAS l'envoi.
        d["is_dev"] = os.environ.get("VLOCAL_DEV") == "1"
        try:
            d["auto_diag"] = _auto_diag_enabled()   # v1.0.11 — état effectif du toggle
        except Exception:
            d["auto_diag"] = False
        return d

    @_api_safe(default=lambda: {"ok": False, "reason": "error"})
    def send_support_diagnostic(self, note="", email="", source="app-diag"):
        """v1.0.10 — Envoi d'un diagnostic au support DIRECTEMENT depuis l'app.
        Garantie : AUCUNE donnée personnelle. Pas d'audio, pas de texte dicté
        (events.jsonl exclut déjà 'text'), pas de chemin de fichier (le
        models_dir, qui contient le nom d'utilisateur macOS, est EXCLU ici),
        pas de hostname. On n'envoie que : version, OS/arch, RAM, état
        moteur/GPU, et des compteurs/codes d'incidents anonymes. L'email est
        FACULTATIF (uniquement si l'utilisateur veut une réponse). Le tout part
        dans la table feedback via l'edge submit-feedback (-> admin + notif
        fondateur). Réutilise le canal feedback existant, rien de neuf côté DB."""
        import urllib.request as _u, platform as _pf
        note = (note or "").strip()
        email = (email or "").strip()[:200]
        if len(note) < 3:
            return {"ok": False, "reason": "empty"}
        if len(note) > 3000:
            note = note[:3000]
        # Diagnostic SANITISÉ (zéro chemin, zéro texte, zéro nom d'utilisateur).
        try:
            d = self.get_diagnostics()
        except Exception:
            d = {}
        if not isinstance(d, dict):
            d = {}
        lines = [
            "Vlocal " + str(d.get("version") or appstate.app_version),
            "System : " + str(d.get("arch") or "?") + " / macOS "
                + str(d.get("macos") or "?") + " / " + str(d.get("ram_gb") or "?") + " GB",
            "Engine : " + ("ready" if d.get("engine_ready") else "loading")
                + " / GPU : " + ("MLX" if d.get("gpu_mlx") else "CPU"),
            "Models : " + ("download pending" if d.get("models_need_download") else "present"),
            "Lang : " + str(d.get("lang") or "?"),
        ]
        try:
            lines.append("Errors : " + json.dumps(d.get("errors_summary") or {}))
        except Exception:
            pass
        ev = d.get("recent_events") or []
        if isinstance(ev, list) and ev:
            lines.append("Recent events :")
            for e in ev[-15:]:
                try:
                    lines.append("  " + str(e.get("ts") or "") + "  " + str(e.get("code") or ""))
                except Exception:
                    pass
        diag = "\n".join(lines)
        message = note + "\n\n— Diagnostic (aucune donnée personnelle) —\n" + diag
        if len(message) > 3900:
            message = message[:3900]
        try:
            os_ver = _pf.mac_ver()[0] or ""
        except Exception:
            os_ver = ""
        payload = {"category": "bug", "message": message,
                   "source": (source or "app-diag")[:20],
                   "app_version": appstate.app_version, "os_version": os_ver}
        if email:
            payload["email"] = email
        url = edge_url("submit-feedback")
        try:
            body = json.dumps(payload).encode("utf-8")
            req = _u.Request(url, data=body, method="POST")
            req.add_header("Content-Type", "application/json")
            for _hk, _hv in auth_headers().items():
                req.add_header(_hk, _hv)
            with _u.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception:
            return {"ok": False, "reason": "network"}
        return {"ok": bool(data.get("ok")), "reason": data.get("reason") or ""}

    # ------------------------ v12c : Réglages MODAL HTML ---------------------
    def open_settings(self):
        """v12c — Ouvre le modal Réglages dans la fenêtre principale.
        Plus de 2e fenêtre webview (pywebview ne supporte pas la création
        après webview.start() de façon fiable). Le modal est dans le HTML
        principal et on l'affiche via JS.

        v13.2 — Refonte robuste après bug terrain : ni le clic menu bar ni
        Cmd+, n'ouvraient le panneau. Cause probable : evaluate_js sur une
        fenêtre cachée pouvait être un no-op silencieux, et l'activation
        Cocoa n'était pas dispatchée sur le main thread depuis tous les
        chemins. On dispatche explicitement la phase Cocoa sur le main
        thread, puis on retente l'injection JS sur 3 frames espacées de
        ~120 ms le temps que la fenêtre soit visible et le DOM prêt.
        """
        _open_settings_modal_robust()
        return True

    def load_glossary(self):
        return _load_glossary_text()

    def save_glossary(self, text):
        return _save_glossary_text(text or "")

    def load_settings(self):
        s = _load_settings()
        # Valeurs par défaut visibles côté UI
        # v20 — défaut ALIGNÉ sur le produit : ctrl_cmd (chord sans bip système,
        # affiché par l'UI et fallback de la table HOTKEYS). L'ancien défaut
        # ctrl_space divergeait des deux.
        s.setdefault("hotkey",
                     os.environ.get("VLOCAL_HOTKEY", "ctrl_cmd"))
        s.setdefault("reunion_enabled", appstate.reunion_enabled)
        s.setdefault("insert_at_cursor", True)         # Vlocal 2
        s.setdefault("dictation_speed", "auto")        # auto | fidele | rapide
        s.setdefault("audio_device", None)             # None = micro par défaut
        s.setdefault("diarization_enabled", True)      # v18 : qui parle quand
        s.setdefault("history_scope", "all")           # v18.4 : all|meetings|none
        s.setdefault("meeting_speakers", "auto")        # WhoTalks : auto fiable
        s.setdefault("obsidian_enabled", False)         # v1.0.13 : capture des dictées dans Obsidian
        s.setdefault("obsidian_vault", None)            # chemin du coffre connecté
        s.setdefault("telemetry_enabled", None)         # v1.1.0 : None = pas encore choisi
        s.setdefault("first_name", "")
        s.setdefault("last_name", "")
        s.setdefault("email", "")                       # v1.2.0 : contact (partagé si accepté)
        return s

    def save_settings(self, patch):
        new = _save_settings(patch or {})
        # v1.3.3 — touche ou mode de déclenchement modifiés : prise d'effet immédiate.
        if any(k in (patch or {}) for k in ("hotkey", "hotkey_mode")):
            threading.Thread(target=_rearm_global_hotkey, name="hotkey-rearm",
                             daemon=True).start()
        # v1.3.4 — Apparence : la bulle de dictée suit le tableau de bord.
        if "theme" in (patch or {}):
            try:
                overlay.set_theme(new.get("theme", "dark"))
            except Exception as _th_err:
                log.warning(f"[overlay] thème non appliqué : {_th_err}")
        # v1.1.0 — partage activé depuis les Réglages : premier envoi tout de suite.
        if (patch or {}).get("telemetry_enabled") is True and appstate.telemetry is not None:
            threading.Thread(target=appstate.telemetry.sync_now, name="telemetry-now",
                             daemon=True).start()
        # Application en chaud quand possible
        try:
            if "reunion_enabled" in patch:
                appstate.reunion_enabled = bool(patch["reunion_enabled"])
                # Synchroniser l'UI principale si ouverte
                _ui("if(typeof _applyReunionFlag==='function')"
                    "_applyReunionFlag(%s);" %
                    ("true" if appstate.reunion_enabled else "false"))
        except Exception:
            pass
        return new

    # ── v1.0.13 — Connexion Obsidian (capture locale des dictées, ADDITIF) ───
    @_api_safe(default=lambda: {"enabled": False, "path": None, "valid": False})
    def obsidian_state(self):
        """État de la connexion Obsidian (lu depuis settings.json)."""
        s = _load_settings()
        path = s.get("obsidian_vault")
        return {"enabled": bool(s.get("obsidian_enabled")),
                "path": path,
                "valid": bool(path) and os.path.isdir(path)}

    @_api_safe(default=lambda: {"ok": False, "error": "Création du coffre impossible."})
    def create_obsidian_vault(self):
        """v1.1.0 — Crée le coffre « Vlocal, ma voix » dans Documents (ou le
        complète s'il existe), l'inscrit dans Obsidian et le connecte. Tout ce qui
        est dit dans Vlocal y est ensuite rangé : dictées, réunions, rappels."""
        r = obsidian.create_voice_vault()
        if not r.get("ok"):
            return {"ok": False, "error": r.get("error") or "Création du coffre impossible."}
        _save_settings({"obsidian_vault": r["path"], "obsidian_enabled": True})
        return {"ok": True, "path": r["path"], "created": bool(r.get("created"))}

    @_api_safe(default=lambda: {"ok": False, "error": "Connexion à Obsidian impossible."})
    def connect_obsidian(self):
        """Détecte AUTOMATIQUEMENT le coffre Obsidian (lit la config d'Obsidian,
        donc indépendant de la version installée) et s'y branche. 100% local. Si
        aucun coffre n'est détecté, l'utilisateur le choisit (pick_obsidian_vault)."""
        try:
            import obsidian as _obs
        except Exception:
            return {"ok": False, "error": "Module Obsidian indisponible."}
        path = _obs.detect_vault()
        if not path:
            return {"ok": False,
                    "error": "Aucun coffre Obsidian détecté. Choisissez-le manuellement."}
        _save_settings({"obsidian_vault": path, "obsidian_enabled": True})
        try:
            _obs.seed_routing(path)
        except Exception:
            pass
        return {"ok": True, "path": path, "auto": True}

    @_api_safe(default=lambda: {"ok": False, "error": "Connexion à Obsidian impossible."})
    def pick_obsidian_vault(self):
        """Choix MANUEL du coffre via le dialogue de dossier natif (repli si la
        détection auto n'a rien trouvé, ou pour changer de coffre)."""
        try:
            import webview as _wv
            dest = appstate.window.create_file_dialog(
                _wv.FOLDER_DIALOG, directory=os.path.expanduser("~"))
        except Exception:
            return {"ok": False, "error": "Connexion à Obsidian impossible."}
        if not dest:
            return {"ok": False, "cancelled": True}
        path = dest if isinstance(dest, str) else (dest[0] if dest else None)
        if not path:
            return {"ok": False, "cancelled": True}
        try:
            import obsidian as _obs
            if not _obs.is_vault(path):
                return {"ok": False, "error": "Ce dossier n'est pas un coffre Obsidian."}
            _save_settings({"obsidian_vault": path, "obsidian_enabled": True})
            _obs.seed_routing(path)
        except Exception:
            return {"ok": False, "error": "Connexion à Obsidian impossible."}
        return {"ok": True, "path": path}

    # ── v1.0 — Activation par clé de licence ────────────────────────────────
    @_api_safe(default=lambda: {"since_day": None, "total": {}, "month": {}, "series": []})
    def usage_stats(self):
        """v1.2.0 — Chiffres EXACTS de l'accueil : lus dans les compteurs
        (usage_days), pas recalculés sur les entrées d'historique affichées.
        L'ancien calcul ne voyait que les 400 dernières et rien du tout si
        l'historique était désactivé : sur une base réelle il affichait 50 min
        là où l'utilisateur avait gagné 4 h 05 dans le mois."""
        return appstate.store.usage_stats() if appstate.store is not None else {
            "since_day": None, "total": {}, "month": {}, "series": []}

    # ------------------- v1.1.0 : identité et télémétrie déclarée -------------
    def telemetry_state(self):
        """Ce que l'utilisateur a choisi de partager, et les compteurs locaux.
        `enabled` vaut None tant qu'aucun choix n'a été fait (l'UI le demande)."""
        try:
            s = _load_settings()
            totals = appstate.store.usage_totals() if appstate.store is not None else {}
            return {"enabled": s.get("telemetry_enabled"),
                    "first_name": s.get("first_name") or "",
                    "last_name": s.get("last_name") or "",
                    "email": s.get("email") or "",
                    "install_id": s.get("install_id") or "",
                    "last_sync": float(s.get("telemetry_last_sync") or 0),
                    "totals": totals}
        except Exception:
            return {"enabled": None, "first_name": "", "last_name": "",
                    "email": "", "install_id": "", "last_sync": 0, "totals": {}}

    def save_identity(self, first_name="", last_name="", email="", enabled=True):
        """Enregistre prénom, nom, e-mail et le choix de partage. Si le partage
        est activé, un envoi part tout de suite (thread, jamais bloquant).
        L'e-mail est validé ici comme côté serveur : une valeur mal formée est
        refusée plutôt que stockée."""
        email = (email or "").strip().lower()[:200]
        if enabled and email and not telemetry.valid_email(email):
            return {"ok": False, "error": _te("Cette adresse e-mail n'est pas valide.")}
        patch = {"first_name": (first_name or "").strip()[:80],
                 "last_name": (last_name or "").strip()[:80],
                 "email": email,
                 "telemetry_enabled": bool(enabled)}
        try:
            _save_settings(patch)
        except Exception:
            return {"ok": False}
        if patch["telemetry_enabled"] and appstate.telemetry is not None:
            threading.Thread(target=appstate.telemetry.sync_now, name="telemetry-now",
                             daemon=True).start()
        return {"ok": True}
