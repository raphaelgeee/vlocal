"""API du tableau de bord : mises à jour, permissions, notifications, fenêtre.

Méthodes appelées par le JavaScript (window.pywebview.api.*). La mise à jour
in-app se fait en deux temps : update_prepare() télécharge, vérifie et prépare
la nouvelle version dans un thread (l'état est poussé à l'interface),
update_apply() lance l'installateur détaché et ferme Vlocal.
"""
import json
import os
import sys
import threading

import applog
import appstate
import notifier
import permissions
import updater
from lifecycle import _quit_app, _reveal_app_in_finder
from supabase_config import auth_headers, edge_url
from texts import _te
from ui_bridge import _ui

log = applog.get("api_system")

_update_busy = False       # v1.0.5 — une préparation de MAJ est en cours (anti-double)
_update_staged = None      # v1.0.5 — chemin de la réplique vérifiée prête à installer


class SystemApi:
    """Mises à jour, permissions macOS, notifications et fenêtre."""

    def check_updates(self):
        """v1.0.3 — Y a-t-il une version plus récente ? + éventuel message d'annonce
        (broadcast admin) via get-latest-version. Fail-open total : hors-ligne ->
        offline=True (on n'affirme PAS « à jour »), aucune donnée utilisateur envoyée
        (simple GET). C'est ce qui rend la base installée « rattrapable »."""
        import urllib.request as _u
        cur = appstate.app_version
        out = {"current": cur, "latest": cur, "update": False, "offline": False,
               "url": "https://www.vlocal.org/telecharger", "notes": "", "message": "",
               "length": 0, "sha256": None}
        url = edge_url("get-latest-version")
        try:
            req = _u.Request(url)
            for _hk, _hv in auth_headers().items():
                req.add_header(_hk, _hv)
            with _u.urlopen(req, timeout=6) as resp:
                d = json.loads(resp.read().decode("utf-8"))
            latest = (d.get("version") or cur).strip()
            out["latest"] = latest
            out["notes"] = (d.get("notes") or "").strip()
            out["message"] = (d.get("message") or d.get("admin_message") or "").strip()
            if d.get("url"):
                out["url"] = d.get("url")
            try:
                out["length"] = int(d.get("length") or 0)
            except Exception:
                log.debug("int : échec ignoré", exc_info=True)
                out["length"] = 0
            # Empreinte du DMG publiée avec la version : updater.download la vérifie.
            out["sha256"] = d.get("sha256")
            # Une version qui a déjà échoué à démarrer sur ce Mac (retour arrière)
            # n'est plus proposée : l'app bouclerait sur la même panne.
            try:
                pa = [int(x) for x in latest.split(".")]
                pb = [int(x) for x in cur.split(".")]
                out["update"] = pa > pb and not updater.is_blocked(latest)
            except Exception:
                log.debug("int : échec ignoré", exc_info=True)
                out["update"] = (latest != cur) and not updater.is_blocked(latest)
        except Exception:
            log.debug("_u.Request : échec ignoré", exc_info=True)
            out["offline"] = True
        return out

    def update_prepare(self):
        """v1.0.5 — MAJ IN-APP, étape 1/2 : télécharge le DMG de la dernière
        version, VÉRIFIE qu'il est signé Developer ID + notarisé Apple, puis en
        prépare une réplique exacte hors du DMG. Tourne en THREAD (la promesse JS
        revient tout de suite) et pousse l'état vers l'UI :
          setUpdateProgress(frac,label) / updateStage(name) /
          updateReady(version) / updateError(msg).
        Rien n'est installé ici : c'est update_apply() qui déclenche le swap."""
        global _update_busy
        if _update_busy:
            return {"ok": False, "busy": True}
        info = self.check_updates()
        if info.get("offline"):
            _ui("if(typeof updateError==='function') updateError(%s);"
                % json.dumps(_te("Connexion impossible. Réessayez plus tard.")))
            return {"ok": False, "offline": True}
        if not info.get("update"):
            return {"ok": False, "uptodate": True}
        url = info.get("url") or ""
        ver = info.get("latest") or ""
        length = info.get("length") or 0
        if not url.startswith("https://"):
            _ui("if(typeof updateError==='function') updateError(%s);"
                % json.dumps(_te("Lien de mise à jour invalide.")))
            return {"ok": False}
        _update_busy = True

        def _work():
            global _update_busy, _update_staged
            try:
                def _prog(frac, label):
                    _ui("if(typeof setUpdateProgress==='function') setUpdateProgress(%.4f,%s);"
                        % (float(frac), json.dumps(label)))

                def _stage(name):
                    _ui("if(typeof updateStage==='function') updateStage(%s);"
                        % json.dumps(name))

                _stage("download")
                dmg = updater.download(url, length, ver, on_progress=_prog,
                                       sha256=info.get("sha256"))
                staged = updater.verify_and_stage(dmg, on_stage=_stage,
                                                  expected_version=ver,
                                                  current_version=appstate.app_version)
                _update_staged = staged
                _ui("if(typeof updateReady==='function') updateReady(%s);"
                    % json.dumps(ver))
            except updater.UpdateError as e:
                _ui("if(typeof updateError==='function') updateError(%s);"
                    % json.dumps(e.message(appstate.ui_lang)))
            except Exception as e:  # noqa: BLE001
                log.warning(f"[update] prepare KO : {e}")
                _ui("if(typeof updateError==='function') updateError(%s);"
                    % json.dumps(_te("La mise à jour a échoué. Réessayez.")))
            finally:
                _update_busy = False

        threading.Thread(target=_work, daemon=True).start()
        return {"ok": True, "started": True, "version": ver}

    def update_apply(self):
        """v1.0.5 — MAJ IN-APP, étape 2/2 : lance l'installateur détaché (qui
        attend la mort de l'app, remplace le bundle en 2 renames atomiques —
        JAMAIS deux versions — puis ROUVRE Vlocal à jour), affiche « Vlocal va
        se fermer » et termine ce process. Les modèles ne sont pas touchés."""
        staged = _update_staged
        if not staged or not os.path.isdir(staged):
            return {"ok": False, "error": _te("Mise à jour non préparée.")}
        try:
            updater.apply_update(staged, os.getpid(), from_version=appstate.app_version)
        except updater.UpdateError as e:
            return {"ok": False, "error": e.message(appstate.ui_lang)}
        except Exception as e:  # noqa: BLE001
            log.warning(f"[update] apply KO : {e}")
            return {"ok": False, "error": _te("L'installation a échoué.")}

        # laisse l'UI peindre l'écran « fermeture » avant de mourir ; l'installateur
        # détaché attend la mort du process puis rouvre l'app à jour.
        threading.Timer(1.2, _quit_app).start()
        return {"ok": True}

    def open_url(self, url):
        """v1.0.3 — Ouvre une URL https de confiance (téléchargement d'une MAJ)
        dans le navigateur par défaut."""
        try:
            u = (url or "").strip()
            # https (téléchargement MAJ) + mailto (lien support@ -> client mail). Rien d'autre.
            if u.startswith("https://") or u.startswith("mailto:"):
                import subprocess
                subprocess.Popen(["open", u])
        except Exception:
            log.debug("(url or '').strip : échec ignoré", exc_info=True)
        return {"ok": True}

    # ------------------- v3.1 : onboarding intégré (permissions) -------------
    def perms_state(self):
        """v3.1 — État des permissions macOS pour l'étape Autorisations de
        l'onboarding. mic: 0=non déterminé, 2=refusé, 3=accordé."""
        try:
            acc = bool(permissions.accessibility_ok())
        except Exception:
            log.debug("bool : échec ignoré", exc_info=True)
            acc = False
        try:
            mic = int(permissions.mic_status())
        except Exception:
            log.debug("int : échec ignoré", exc_info=True)
            mic = 3
        return {"accessibility": acc, "mic": mic}

    def open_perm_settings(self, pane):
        """v3.1 — Bouton « Ouvrir les Réglages » de l'onboarding. Micro non
        déterminé -> dialogue natif ; sinon -> bon panneau Réglages Système."""
        p = "microphone" if str(pane or "").lower().startswith("mic") else "accessibility"
        try:
            if p == "microphone":
                permissions.mic_prompt()        # tentative propre AVFoundation (no-op si déjà décidé)
                # v1.0.13 — DÉCLENCHEUR FIABLE du prompt micro : on ouvre brièvement
                # un flux d'entrée (LE mécanisme de la dictée, prouvé sur le notarisé).
                # requestAccessForMediaType seul ne déclenchait pas toujours le dialogue
                # -> onboarding frais bloqué (app jamais inscrite dans Réglages > Micro,
                # panneau sans « + »). L'ouverture du flux force le prompt + inscrit l'app.
                def _trig_mic():
                    try:
                        import sounddevice as _sd, time as _tt
                        s = _sd.InputStream(samplerate=16000, channels=1,
                                            dtype="float32", blocksize=1600)
                        s.start(); _tt.sleep(0.3)
                        try:
                            s.stop()
                        except Exception:
                            log.debug("s.stop : échec ignoré", exc_info=True)
                        try:
                            s.close()
                        except Exception:
                            log.debug("s.close : échec ignoré", exc_info=True)
                    except Exception as _e:
                        log.warning(f"[perm] déclencheur micro KO : {_e}")
                threading.Thread(target=_trig_mic, name="mic-perm-trigger", daemon=True).start()
                # Déjà demandé mais refusé/restreint -> ouvrir les Réglages (l'app y est).
                try:
                    if (not permissions.mic_ok()) and permissions.mic_status() != 0:
                        permissions.open_settings("microphone")
                except Exception:
                    log.debug("permissions.open_settings : échec ignoré", exc_info=True)
            else:
                # Déclenche le dialogue système (AXIsProcessTrustedWithOptions) :
                # inscrit Vlocal dans la liste Accessibilité de TCC (sinon il
                # peut ne PAS y apparaître) + ouvre le bon panneau.
                permissions.accessibility_prompt()
                permissions.open_settings("accessibility")
        except Exception:
            log.debug("permissions.mic_prompt : échec ignoré", exc_info=True)
        return {"ok": True}

    def arm_hotkey(self):
        """v3.1 — Après l'octroi de l'Accessibilité, le raccourci se RÉ-ARME TOUT
        SEUL via l'auto-réparation (hotkey_mac._watch_trust surveille
        AXIsProcessTrusted toutes les 2 s et réinstalle les moniteurs). On NE
        rappelle SURTOUT PAS start_global_hotkey() ici : ça empilerait une 2e paire
        de moniteurs NSEvent -> dictée déclenchée en double. No-op volontaire :
        l'UI peut l'appeler sans risque (le raccourci s'active sous ~2 s)."""
        return {"ok": True}

    def relaunch(self):
        """v1.0.4 — Relance Vlocal PROPREMENT. INDISPENSABLE après l'octroi de
        l'Accessibilité : macOS NE met PAS à jour AXIsProcessTrusted dans un
        process DÉJÀ lancé (comportement Ventura 13.0+), donc l'insertion au
        curseur reste bloquée tant que l'app n'a pas redémarré. On quitte ce
        process puis on ouvre une instance NEUVE qui, elle, voit la permission.
        Le client ne fait qu'UN clic « Redémarrer » — pas besoin de connaître
        Cmd+Q."""
        try:
            import shlex
            try:
                from Foundation import NSBundle
                path = NSBundle.mainBundle().bundlePath() or "/Applications/Vlocal.app"
            except Exception:
                log.debug("import Foundation : échec ignoré", exc_info=True)
                path = "/Applications/Vlocal.app"
            import subprocess
            # le shell attend que CE process meure (sleep), puis ouvre du NEUF (-n)
            # v1.3.2 — on attend la MORT RÉELLE de ce process (20 s au plus) avant de
            # rouvrir. Avec un « sleep 2 » fixe, une fermeture un peu lente laissait
            # la nouvelle instance buter sur le verrou d'instance unique et
            # s'éteindre : Vlocal disparaissait sans revenir.
            pid = os.getpid()
            subprocess.Popen(["/bin/sh", "-c",
                              "i=0; while kill -0 %d 2>/dev/null && [ $i -lt 100 ]; "
                              "do sleep 0.2; i=$((i+1)); done; /usr/bin/open %s"
                              % (pid, shlex.quote(path))])
            # quit RÉEL sur le main thread (sinon l'ancien process survit et
            # `open -n` ouvrirait une 2e instance -> 2 icônes + insertion figée).
            _quit_app()
        except Exception as e:
            log.warning(f"[relaunch] KO : {e}")
        return {"ok": True}

    def repair_insertion(self):
        """v1.0.7 — RÉPARATION INSERTION EN 1 CLIC (zéro Terminal). Le cas vécu :
        après une réinstall / un changement de signature, la case « Vlocal » reste
        cochée dans Accessibilité mais N'AUTORISE PLUS le nouveau binaire (entrée
        TCC « fantôme »). On l'EFFACE (`tccutil reset Accessibility com.vlocal.app`,
        sans sudo, autorisé sous hardened runtime), puis on ré-inscrit Vlocal et on
        ouvre le panneau Accessibilité. L'utilisateur n'a plus qu'à RE-cocher Vlocal
        (macOS interdit à une app de s'auto-autoriser : sécurité), puis l'UI propose
        « Terminer » -> relaunch() prend en compte la coche et l'insertion repart.
        Idempotent, best-effort, ne lève jamais."""
        try:
            import subprocess
            subprocess.run(["tccutil", "reset", "Accessibility", "com.vlocal.app"],
                           check=False, capture_output=True, timeout=10)
            log.info("[repair] autorisation Accessibilité réinitialisée (case fantôme effacée).")
        except Exception as e:
            log.warning(f"[repair] tccutil reset KO : {e}")
        try:
            # Ré-inscrit Vlocal dans la liste TCC (best-effort ; macOS met le statut
            # de confiance en cache PAR PROCESS, donc la ré-inscription dans CE
            # process peut ne pas suffire — cf. relaunch()). On ouvre le panneau ET
            # on révèle Vlocal.app dans le Finder : si l'app n'apparaît PAS dans la
            # liste, l'utilisateur la glisse sur le « + » (chemin 100 % fiable).
            # v1.0.18 — Fix 1 : la révélation Finder + le guidage « + » règlent le
            # cas vécu « Vlocal introuvable dans les Réglages » après un reset.
            permissions.accessibility_prompt()
            permissions.open_settings("accessibility")
            _reveal_app_in_finder()
        except Exception as e:
            log.warning(f"[repair] ouverture Réglages Accessibilité KO : {e}")
        return {"ok": True}

    # ------------------- v1.0.6 : diagnostic notifications (rappels) ---------
    def notif_status(self):
        """v1.0.6 — Statut RÉEL de l'autorisation de notification pour Vlocal.
        Sert l'encart Réglages > Rappels (« les rappels sonnent-ils ? »).
        Renvoie un CODE stable, le JS i18n rend le libellé."""
        try:
            st = notifier.auth_status()
        except Exception:
            log.debug("notifier.auth_status : échec ignoré", exc_info=True)
            st = "unknown"
        return {"status": st}

    def test_notification(self):
        """v1.0.6 — Bouton « Tester la notification » des Réglages. Si
        l'autorisation n'a jamais été décidée, on déclenche le prompt natif et on
        attend ; puis on envoie une notif de test (icône Vlocal) et on renvoie le
        statut final + si l'envoi a pu partir. Tourne hors main thread (appel JS)."""
        sent = False
        try:
            st = notifier.auth_status()
            if st == "notDetermined":
                notifier.request_authorization()
                st = notifier.auth_status()
            # On tente l'envoi : si refusé, notifier replie (applet/osascript) et
            # la bannière in-app reste de toute façon le filet pour les rappels.
            sent = bool(notifier.show(
                _te("Test de notification"),
                _te("Si vous voyez ceci, les rappels Vlocal fonctionnent."),
                subtitle=None))
        except Exception as e:
            log.warning(f"[notif] test KO : {e}")
            st = "unknown"
        return {"status": st, "sent": sent}

    def open_notif_settings(self):
        """v1.0.6 — Ouvre Réglages Système > Notifications (pour réactiver Vlocal
        si l'utilisateur avait refusé). macOS ne deep-linke pas par app de façon
        fiable -> on ouvre le panneau Notifications, Vlocal y est listé."""
        try:
            if sys.platform == "darwin":
                from AppKit import NSWorkspace
                from Foundation import NSURL
                url = NSURL.URLWithString_(
                    "x-apple.systempreferences:com.apple.Notifications-Settings.extension")
                NSWorkspace.sharedWorkspace().openURL_(url)
        except Exception as e:
            log.warning(f"[notif] ouverture Réglages Notifications KO : {e}")
        return {"ok": True}

    def hide_window(self):
        """v7 — masque la fenêtre sans tuer le process. L'app reste vivante via
        le menu bar (clic 'Ouvrir Vlocal' pour la rouvrir)."""
        try:
            if appstate.window is not None:
                appstate.window.hide()
                appstate.window_visible = False
        except Exception:
            log.debug("window.hide : échec ignoré", exc_info=True)
        return True
