"""Porte de build : le cycle de vie de la VRAIE application.

    ./venv/bin/python tests/lifecycle_smoke.py

Lance app.main() tel qu'il tourne chez les utilisateurs, dans un HOME temporaire
(verrou d'instance, réglages, base et journaux séparés : l'instance de Vlocal
ouverte sur la machine n'est jamais touchée), puis vérifie sur la vraie fenêtre :

  1. le raccourci global a démarré, sans exception ;
  2. l'icône V de la barre des menus existe ;
  3. le délégué d'application sait rouvrir la fenêtre ;
  4. fermer la fenêtre la MASQUE (le raccourci reste vivant) ;
  5. rouvrir la ré-affiche ;
  6. quitter termine vraiment le processus.

Pourquoi : en 1.2.0 et 1.3.0, start_global_hotkey() levait à chaque lancement
(UnboundLocalError) et emportait tout le démarrage avec lui. L'app s'ouvrait,
la fenêtre fonctionnait, le bouton Dicter aussi : aucun test ne voyait rien.
Chez l'utilisateur, le raccourci ne faisait rien, et une fois la fenêtre fermée
il devenait impossible de rouvrir ou de quitter Vlocal sans forcer.

Isolement : réseau vers R2 et Supabase coupé, invites micro et Accessibilité
neutralisées, moteur Whisper non chargé (inutile ici), télémétrie désactivée.
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DELAI_DEMARRAGE_S = 9.0
DELAI_QUITTER_S = 7.0


def enfant(sortie):
    """Tourne DANS le processus de l'app : pilote la vraie fenêtre."""
    R = {"etapes": []}

    def note(cle, val):
        R[cle] = val
        R["etapes"].append(cle)
        with open(sortie, "w", encoding="utf-8") as f:
            json.dump(R, f, ensure_ascii=False, indent=1)

    sys.path.insert(0, RACINE)
    import urllib.request as _u
    _orig = _u.urlopen

    def _reseau_coupe(req, *a, **k):
        url = req if isinstance(req, str) else getattr(req, "full_url", "")
        if "r2.dev" in url or "supabase.co" in url:
            raise OSError("réseau coupé par la porte de cycle de vie")
        return _orig(req, *a, **k)
    _u.urlopen = _reseau_coupe

    import permissions
    permissions.mic_prompt = lambda *a, **k: None
    permissions.accessibility_prompt = lambda *a, **k: None
    permissions.open_settings = lambda *a, **k: None

    import app
    app._load_engine_async = lambda *a, **k: None       # le moteur n'a rien à faire ici

    _vrai_start = app.start_global_hotkey

    def _start_observe():
        try:
            t = _vrai_start()
            note("raccourci", "démarré" if t is not None else "inactif (renvoie None)")
            return t
        except Exception as e:
            note("raccourci", f"EXCEPTION {type(e).__name__}: {e}")
            raise
    app.start_global_hotkey = _start_observe

    def sur_main(fn, delai=2.5):
        from Foundation import NSOperationQueue
        fait, boite = threading.Event(), {}

        def bloc():
            try:
                boite["v"] = fn()
            except Exception as e:
                boite["e"] = repr(e)
            finally:
                fait.set()
        NSOperationQueue.mainQueue().addOperationWithBlock_(bloc)
        return fait.wait(delai), boite

    def sonde():
        time.sleep(DELAI_DEMARRAGE_S)
        from AppKit import NSApplication
        import webview.platforms.cocoa as C
        nsapp = NSApplication.sharedApplication()
        inst = C.BrowserView.instances.get(getattr(app._window, "uid", "master"))
        if inst is None:
            note("verdict", "fenêtre introuvable")
            os._exit(4)
        win = inst.window

        vivant, b = sur_main(lambda: {
            "delegue_sait_rouvrir": bool(nsapp.delegate() is not None and nsapp.delegate()
                .respondsToSelector_("applicationShouldHandleReopen:hasVisibleWindows:")),
            "icone_barre_des_menus": app._menubar is not None,
            "fenetre_visible": bool(win.isVisible()),
        })
        note("demarrage", b.get("v") if vivant else "boucle d'événements morte")

        sur_main(lambda: win.performClose_(None))
        time.sleep(1.2)
        vivant, b = sur_main(lambda: bool(win.isVisible()))
        note("fermer_masque", (b.get("v") is False) if vivant else "boucle d'événements morte")

        def rouvrir():
            d = nsapp.delegate()
            if d is None or not d.respondsToSelector_("applicationShouldHandleReopen:hasVisibleWindows:"):
                return False
            d.applicationShouldHandleReopen_hasVisibleWindows_(nsapp, False)
            return True
        sur_main(rouvrir)
        time.sleep(1.2)
        vivant, b = sur_main(lambda: bool(win.isVisible()))
        note("rouvrir_affiche", b.get("v") if vivant else "boucle d'événements morte")

        note("quitter", "demandé")
        from Foundation import NSOperationQueue
        NSOperationQueue.mainQueue().addOperationWithBlock_(lambda: nsapp.terminate_(None))
        time.sleep(DELAI_QUITTER_S)
        note("quitter", "ANNULÉ : le processus vit encore")
        os._exit(3)

    threading.Thread(target=sonde, daemon=True).start()
    sys.argv = [sys.argv[0]]
    app.main()


def parent():
    """Prépare un HOME isolé, lance l'enfant, rend le verdict."""
    home = tempfile.mkdtemp(prefix="vlocal-cycle-")
    support = os.path.join(home, "Library", "Application Support", "Vlocal")
    os.makedirs(support)
    os.makedirs(os.path.join(home, "Library", "Logs"))
    vrais_modeles = os.path.expanduser("~/Library/Application Support/Vlocal/models")
    if os.path.isdir(vrais_modeles):
        os.symlink(vrais_modeles, os.path.join(support, "models"))
    with open(os.path.join(support, "settings.json"), "w", encoding="utf-8") as f:
        json.dump({"onboarded": True, "onb_step": 0, "telemetry_enabled": False,
                   "hotkey": "ctrl_cmd", "install_id": "00000000-0000-4000-8000-00000000c1c1"}, f)
    sortie = os.path.join(home, "cycle.json")
    env = dict(os.environ, HOME=home)
    try:
        proc = subprocess.run([sys.executable, os.path.abspath(__file__), "--enfant", sortie],
                              cwd=RACINE, env=env, timeout=DELAI_DEMARRAGE_S + DELAI_QUITTER_S + 30,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        code = proc.returncode
    except subprocess.TimeoutExpired:
        code = "délai dépassé"
    try:
        R = json.load(open(sortie, encoding="utf-8"))
    except Exception:
        R = {}

    d = R.get("demarrage") if isinstance(R.get("demarrage"), dict) else {}
    controles = [
        ("le raccourci global démarre sans exception", R.get("raccourci") == "démarré"),
        ("l'icône V de la barre des menus existe", d.get("icone_barre_des_menus") is True),
        ("le délégué d'application sait rouvrir", d.get("delegue_sait_rouvrir") is True),
        ("fermer la fenêtre la masque", R.get("fermer_masque") is True),
        ("rouvrir la ré-affiche", R.get("rouvrir_affiche") is True),
        ("quitter termine le processus", code == 0 and R.get("quitter") == "demandé"),
    ]
    print("CYCLE DE VIE DE LA VRAIE APP")
    for libelle, ok in controles:
        print(f"  {'OK ' if ok else 'KO '} {libelle}")
    if R.get("raccourci", "").startswith("EXCEPTION"):
        print("  détail :", R["raccourci"])
    print(f"  code de sortie du processus : {code}")
    if not all(ok for _, ok in controles):
        print(f"  HOME isolé conservé pour examen : {home}")
        return 1
    # Succès : rien ne doit rester derrière un build (le lien vers les modèles
    # est un lien symbolique, rmtree ne suit pas les liens et ne touche jamais
    # aux vrais modèles).
    import shutil
    shutil.rmtree(home, ignore_errors=True)
    return 0


if __name__ == "__main__":
    if "--enfant" in sys.argv:
        enfant(sys.argv[sys.argv.index("--enfant") + 1])
    else:
        sys.exit(parent())
