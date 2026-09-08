#!/usr/bin/env python3
"""Vlocal — mise à jour in-app « niveau gros logiciel » (v1.0.5).

OBJECTIF (exigences Raphael) :
  - vérifier / télécharger / installer la MAJ SANS quitter le site, DANS l'app ;
  - GARDER les modèles (ils sont hors-bundle dans Application Support -> jamais
    touchés par une MAJ) ;
  - JAMAIS deux versions chez un client : le bundle est remplacé EN ENTIER
    (miroir exact, zéro fichier périmé), 1 seule entrée LaunchServices ;
  - redémarrage AUTOMATIQUE : on prévient l'utilisateur (« Vlocal va se fermer »),
    le process meurt, un installateur détaché remplace l'app puis la ROUVRE à jour ;
  - SÉCURITÉ : on n'installe JAMAIS du code non vérifié — la nouvelle app est
    contrôlée `codesign --verify --strict` ET `spctl` (notarisation Apple) AVANT
    le moindre remplacement. Un DMG corrompu/tronqué/altéré est rejeté.

POURQUOI maison et pas Sparkle : l'app est un bundle PyInstaller (~664 Mo) sans
projet Xcode -> signer les helpers XPC de Sparkle à la main = enfer + rebuilds.
Ici on réutilise l'infra DÉJÀ éprouvée (téléchargement R2 type model_store +
relaunch) -> ~1 build, aucun framework à signer.

Le pont vers l'UI passe par des callbacks (on_progress / on_stage) fournis par
app.py (qui les route vers _ui) — exactement comme engine.on_progress.
"""
import os
import sys
import subprocess
import threading

SUPPORT = os.path.expanduser("~/Library/Application Support/Vlocal")
UPDATES = os.path.join(SUPPORT, "updates")          # zone de travail des MAJ
TARGET = "/Applications/Vlocal.app"                  # emplacement canonique unique
INCOMING = "/Applications/.Vlocal-incoming.app"      # copie entrante (même volume)
BACKUP = "/Applications/.Vlocal-backup.app"          # filet de restauration

# Cloudflare R2 (r2.dev) renvoie 403 sur l'UA urllib par défaut -> UA explicite
# (même contrainte que model_store).
_UA = "Vlocal-Updater (macOS)"


# v1.1.2 — ÉPINGLAGE de l'éditeur : au-delà de « signé par un Developer ID et
# notarisé », la mise à jour doit être signée par CETTE équipe Apple. Sans ce
# contrôle, quiconque publierait une URL de DMG vers une app notarisée par un
# autre développeur passerait les deux vérifications précédentes.
EXPECTED_TEAM_ID = "LW8B2TTQ4W"


def team_id_matches(codesign_output: str, expected: str = EXPECTED_TEAM_ID) -> bool:
    """True si la sortie de `codesign -dv --verbose=2` porte TeamIdentifier=expected."""
    for line in (codesign_output or "").splitlines():
        line = line.strip()
        if line.startswith("TeamIdentifier="):
            return line.split("=", 1)[1].strip() == expected
    return False


class UpdateError(Exception):
    """Échec MAJ avec message CLAIR destiné à l'utilisateur (déjà en français)."""


def _open(url, timeout):
    import urllib.request
    return urllib.request.urlopen(
        urllib.request.Request(url, headers={"User-Agent": _UA}), timeout=timeout)


def _run(cmd, timeout=180):
    """Lance une commande système, renvoie (code, sortie). Jamais d'exception."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:  # noqa: BLE001
        return 255, str(e)


# --------------------------------------------------------------------------- #
# 1) TÉLÉCHARGEMENT — robuste (atomique .part -> replace, reprise, 3 réessais,
#    progression). Calque exact de model_store.download_models.
# --------------------------------------------------------------------------- #
def download(url, expected_len, version, on_progress=None):
    """Télécharge le DMG de la MAJ vers UPDATES. Vérifie la taille annoncée.
    on_progress(frac 0..1, label). Renvoie le chemin du .dmg. Lève UpdateError."""
    os.makedirs(UPDATES, exist_ok=True)
    dst = os.path.join(UPDATES, "Vlocal-%s.dmg" % version)
    tmp = dst + ".part"
    # reprise gratuite : DMG déjà là et de la bonne taille -> on le réutilise.
    if expected_len and os.path.exists(dst) and os.path.getsize(dst) == int(expected_len):
        if on_progress:
            on_progress(1.0, "Téléchargement terminé")
        return dst
    total = int(expected_len) if expected_len else 0
    last_err = None
    for _attempt in range(3):
        try:
            with _open(url, 120) as resp, open(tmp, "wb") as out:
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    out.write(chunk)
                    if on_progress and total:
                        on_progress(min(out.tell() / total, 1.0), "Téléchargement")
            got = os.path.getsize(tmp)
            if total and got != total:
                raise UpdateError("Téléchargement incomplet (%d/%d octets)." % (got, total))
            os.replace(tmp, dst)
            if on_progress:
                on_progress(1.0, "Téléchargement terminé")
            return dst
        except Exception as e:  # noqa: BLE001
            last_err = e
            try:
                os.remove(tmp)
            except Exception:
                pass
    raise UpdateError("Téléchargement de la mise à jour échoué : %s" % last_err)


# --------------------------------------------------------------------------- #
# 2) VÉRIFICATION + STAGING — on monte le DMG, on PROUVE que l'app est signée
#    Developer ID + NOTARISÉE Apple (offline, via le staple), puis on copie une
#    réplique exacte hors du DMG. Aucune installation tant que ce n'est pas OK.
# --------------------------------------------------------------------------- #
def _detach(mount):
    if mount:
        _run(["/usr/bin/hdiutil", "detach", mount, "-force"], timeout=60)


def verify_and_stage(dmg_path, on_stage=None):
    """Monte le DMG, vérifie l'app (codesign --strict, spctl notarisation, Team ID),
    en copie une réplique exacte dans UPDATES/Vlocal.app. Renvoie ce chemin.
    Lève UpdateError au moindre doute (rien n'est installé)."""
    import plistlib
    if on_stage:
        on_stage("verify")
    # montage non intrusif, lecture seule, sans Finder.
    code, out = _run(["/usr/bin/hdiutil", "attach", "-nobrowse", "-noverify",
                      "-readonly", "-plist", dmg_path], timeout=120)
    if code != 0:
        raise UpdateError("Image de mise à jour illisible (montage échoué).")
    mount = None
    try:
        try:
            pl = plistlib.loads(out.encode("utf-8", "ignore"))
            for ent in pl.get("system-entities", []):
                mp = ent.get("mount-point")
                if mp:
                    mount = mp
                    break
        except Exception:
            # repli : parse texte « /Volumes/... »
            for line in out.splitlines():
                idx = line.find("/Volumes/")
                if idx != -1:
                    mount = line[idx:].strip()
                    break
        if not mount or not os.path.isdir(mount):
            raise UpdateError("Volume de mise à jour introuvable après montage.")
        app = os.path.join(mount, "Vlocal.app")
        if not os.path.isdir(app):
            raise UpdateError("Vlocal.app introuvable dans la mise à jour.")
        # (a) signature Developer ID intègre (hash de CHAQUE fichier vérifié).
        code, out2 = _run(["/usr/bin/codesign", "--verify", "--deep", "--strict",
                          "--verbose=2", app], timeout=120)
        if code != 0:
            raise UpdateError("Signature de la mise à jour invalide — installation refusée.")
        # (b) Gatekeeper : app NOTARISÉE Apple (staple) -> vrai même hors-ligne.
        code, out3 = _run(["/usr/sbin/spctl", "--assess", "--type", "exec",
                          "--verbose=2", app], timeout=120)
        if code != 0:
            raise UpdateError("Mise à jour non notarisée par Apple — installation refusée.")
        # (c) v1.1.2 — l'éditeur est le nôtre (TeamIdentifier épinglé).
        code, out5 = _run(["/usr/bin/codesign", "-dv", "--verbose=2", app], timeout=60)
        if code != 0 or not team_id_matches(out5):
            raise UpdateError("Mise à jour signée par un autre éditeur — installation refusée.")
        # (d) réplique EXACTE hors du DMG (ditto préserve la signature).
        if on_stage:
            on_stage("stage")
        staged = os.path.join(UPDATES, "Vlocal.app")
        _run(["/bin/rm", "-rf", staged], timeout=60)
        code, out4 = _run(["/usr/bin/ditto", app, staged], timeout=300)
        if code != 0 or not os.path.isdir(staged):
            raise UpdateError("Préparation de la mise à jour échouée (copie).")
        return staged
    finally:
        _detach(mount)


# --------------------------------------------------------------------------- #
# 3) INSTALLATION — script détaché qui SURVIT à la mort de l'app : il attend que
#    le process meure, fait le swap en 2 renames atomiques (jamais 2 versions),
#    ré-enregistre 1 seule entrée LaunchServices, puis ROUVRE l'app à jour.
#    Auto-réparation en préambule si une MAJ précédente a été interrompue.
# --------------------------------------------------------------------------- #
_INSTALLER = r'''#!/bin/sh
# Vlocal — installateur de mise à jour (détaché, survit à la fermeture de l'app).
PID="$1"; STAGED="$2"; LOG="$3"
TARGET="/Applications/Vlocal.app"
INCOMING="/Applications/.Vlocal-incoming.app"
BAK="/Applications/.Vlocal-backup.app"
log(){ echo "[$(date '+%H:%M:%S')] $*" >>"$LOG" 2>/dev/null; }

# 0) auto-réparation d'une MAJ interrompue (target manquante) AVANT toute chose
if [ ! -d "$TARGET" ]; then
  if [ -d "$INCOMING" ]; then mv "$INCOMING" "$TARGET"; log "repair: incoming->target";
  elif [ -d "$BAK" ]; then mv "$BAK" "$TARGET"; log "repair: bak->target"; fi
fi

# 1) attendre la mort du process Vlocal (max ~25 s) — sinon fichiers verrouillés
i=0; while kill -0 "$PID" 2>/dev/null && [ $i -lt 125 ]; do sleep 0.2; i=$((i+1)); done

# 2) copie entrante (longue mais NON destructive : on ne touche pas encore target)
rm -rf "$INCOMING" "$BAK"
if ! /usr/bin/ditto "$STAGED" "$INCOMING"; then
  log "ditto staged->incoming KO"; /usr/bin/open -n "$TARGET" 2>/dev/null; exit 1
fi

# 3) SWAP = 2 renames atomiques sur le même volume -> il n'existe JAMAIS 2 Vlocal
if [ -d "$TARGET" ]; then
  if ! mv "$TARGET" "$BAK"; then
    log "mv target->bak KO (droits /Applications ?)"; rm -rf "$INCOMING"
    /usr/bin/open -n "$TARGET" 2>/dev/null; exit 1
  fi
fi
if ! mv "$INCOMING" "$TARGET"; then
  log "mv incoming->target KO -> restauration"
  [ -d "$BAK" ] && mv "$BAK" "$TARGET"
  /usr/bin/open -n "$TARGET" 2>/dev/null; exit 1
fi

# 4) finitions : retirer la quarantaine + 1 SEULE entrée LaunchServices (anti-doublon)
rm -rf "$BAK" "$STAGED"
/usr/bin/xattr -dr com.apple.quarantine "$TARGET" 2>/dev/null
LSR="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
[ -x "$LSR" ] && "$LSR" -f "$TARGET" >/dev/null 2>&1

# 5) relancer la version NEUVE (process frais = zéro ancien code en mémoire)
/usr/bin/open -n "$TARGET" 2>/dev/null
log "MAJ installée -> relance OK"
exit 0
'''


def apply_update(staged_path, app_pid):
    """Écrit l'installateur, le lance DÉTACHÉ (start_new_session = survit au quit)
    puis rend la main : app.py affiche « Vlocal va se fermer » et termine le
    process. Renvoie True si l'installateur est bien lancé."""
    if not staged_path or not os.path.isdir(staged_path):
        raise UpdateError("Mise à jour non préparée (rien à installer).")
    os.makedirs(UPDATES, exist_ok=True)
    sh = os.path.join(UPDATES, "install.sh")
    log = os.path.join(UPDATES, "install.log")
    with open(sh, "w") as f:
        f.write(_INSTALLER)
    os.chmod(sh, 0o755)
    # détaché : start_new_session=True -> survit à la mort de l'app parente.
    subprocess.Popen(["/bin/sh", sh, str(app_pid), staged_path, log],
                     start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True


# --------------------------------------------------------------------------- #
# 4) AUTO-RÉPARATION AU DÉMARRAGE — si on tourne, c'est qu'on a démarré depuis
#    TARGET : les éventuels restes d'une MAJ (backup/incoming) sont périmés.
# --------------------------------------------------------------------------- #
_LSR = ("/System/Library/Frameworks/CoreServices.framework/Frameworks/"
        "LaunchServices.framework/Support/lsregister")


def _self_bundle():
    """Chemin du .app qui EXÉCUTE ce process (PyInstaller : sys.executable =
    .../Vlocal.app/Contents/MacOS/Vlocal). None hors d'un bundle (mode source)."""
    try:
        p = os.path.realpath(sys.executable)
    except Exception:
        return None
    marker = ".app/Contents/MacOS/"
    i = p.find(marker)
    return p[:i + 4] if i != -1 else None


def _dedupe_icons():
    """GARANTIT UNE SEULE ICÔNE (v1.0.14). Désenregistre de LaunchServices tout
    bundle « Vlocal.app » qui n'est PAS celui en cours d'exécution : DMG resté
    monté (/Volumes/Vlocal), copie dans Téléchargements, restes de MAJ, temp.
    NE SUPPRIME AUCUN FICHIER (retire seulement l'entrée LS). macOS récent n'a
    plus `lsregister -kill` -> on cible chaque chemin avec `-u`. Constaté : un
    testeur s'est retrouvé avec 3 icônes après une mise à jour."""
    if not os.path.exists(_LSR):
        return 0
    self_b = _self_bundle()
    if not self_b:
        return 0  # mode source/dev : on ne touche à rien
    self_real = os.path.realpath(self_b)
    code, out = _run([_LSR, "-dump"], timeout=90)
    if code != 0:
        return 0
    removed, seen = 0, set()
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("path:"):
            continue
        m = line.split("path:", 1)[1].strip()
        sp = m.rfind(" (0x")
        if sp != -1:
            m = m[:sp].strip()
        # UNIQUEMENT les bundles nommés exactement « Vlocal.app » (pas le dossier
        # WebKit com.vlocal.app, ni autre chose).
        if os.path.basename(m) != "Vlocal.app" or m in seen:
            continue
        seen.add(m)
        if os.path.realpath(m) == self_real:
            continue  # NE JAMAIS désenregistrer le bundle qui tourne
        _run([_LSR, "-u", m], timeout=30)
        removed += 1
    # (ré)enregistre le bundle courant comme l'unique référence.
    _run([_LSR, "-f", self_b], timeout=30)
    if removed:
        _run(["/usr/bin/killall", "Dock"], timeout=10)  # rafraîchit le Dock 1 fois
    return removed


def cleanup_leftovers():
    """À appeler au lancement : nettoie les restes d'une MAJ (garantit 1 bundle).
    Sûr : l'installateur ne ROUVRE l'app qu'APRÈS avoir lui-même retiré ces
    dossiers ; s'ils existent ici, c'est une MAJ antérieure interrompue."""
    for leftover in (BACKUP, INCOMING):
        try:
            if os.path.isdir(leftover):
                _run(["/bin/rm", "-rf", leftover], timeout=60)
        except Exception:
            pass
    # purge des DMG/staging d'anciennes MAJ (on garde l'espace propre).
    try:
        if os.path.isdir(UPDATES):
            for name in os.listdir(UPDATES):
                if name.endswith(".part"):
                    try:
                        os.remove(os.path.join(UPDATES, name))
                    except Exception:
                        pass
    except Exception:
        pass
    # v1.0.14 — dé-duplication des icônes EN TÂCHE DE FOND (lsregister -dump peut
    # prendre 1-2 s : on ne bloque jamais le lancement de l'UI). Garantit 1 icône.
    try:
        threading.Thread(target=_dedupe_icons, daemon=True).start()
    except Exception:
        pass
