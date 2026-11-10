#!/usr/bin/env python3
"""Vlocal — mise à jour in-app.

Parcours : download() -> verify_and_stage() -> apply_update() -> (relance)
-> cleanup_leftovers() au démarrage suivant -> confirm_startup().

GARANTIES (une mise à jour ratée ne laisse JAMAIS l'utilisateur sans app qui
démarre, ne touche jamais à ses données, ne boucle jamais) :
  - téléchargement repris après coupure (Range), délai par lecture, vitesse
    nulle détectée, espace disque vérifié AVANT, .part puis renommage atomique,
    taille exacte, SHA-256 si le serveur la fournit (model_store.fetch_resumable) ;
  - on n'installe JAMAIS du code non vérifié : signature Developer ID stricte,
    Team ID épinglé, notarisation Apple (spctl, ticket agrafé), identifiant et
    version du bundle (pas de retour en arrière ni de réinstallation), Mach-O
    arm64 ; le DMG est toujours démonté, même en cas d'exception ;
  - remplacement par copie à côté puis deux renommages sur le même volume ;
    l'ancienne version reste en sauvegarde jusqu'à ce que la nouvelle confirme
    son démarrage, sinon RETOUR ARRIÈRE automatique et la version fautive n'est
    plus jamais proposée sur ce Mac ;
  - un seul processus de mise à jour à la fois (verrou fichier côté Python,
    verrou par mkdir côté installateur) ;
  - nettoyage limité à nos propres fichiers, désignés nommément.

POURQUOI maison et pas Sparkle : l'app est un bundle PyInstaller sans projet
Xcode ; signer les helpers XPC de Sparkle à la main coûterait un rebuild à chaque
réglage. Les modèles vivent hors du bundle (App Support) : une mise à jour ne
les touche jamais.

Le pont vers l'UI passe par des callbacks (on_progress / on_stage) fournis par
app.py, comme engine.on_progress. Chaque échec lève UpdateError, qui porte un
code stable, une phrase FR/EN pour l'utilisateur et le détail technique, déjà
écrit au journal.
"""
import json
import os
import plistlib
import shlex
import struct
import subprocess
import sys
import threading
import time

import model_store

SUPPORT = os.path.expanduser("~/Library/Application Support/Vlocal")
UPDATES = os.path.join(SUPPORT, "updates")          # zone de travail des MAJ
TARGET = "/Applications/Vlocal.app"                  # emplacement canonique
APP_NAME = "Vlocal.app"
BUNDLE_ID = "com.vlocal.app"

# v1.1.2 — ÉPINGLAGE de l'éditeur : au-delà de « signé par un Developer ID et
# notarisé », la mise à jour doit être signée par CETTE équipe Apple. Sans ce
# contrôle, quiconque publierait une URL de DMG vers une app notarisée par un
# autre développeur passerait les deux vérifications précédentes.
EXPECTED_TEAM_ID = "LW8B2TTQ4W"

# Démarrages de la nouvelle version sans confirmation avant retour arrière, quand
# le chien de garde de l'installateur n'est plus là (redémarrage du Mac pendant
# l'attente). Le chien de garde, lui, décide en quelques dizaines de secondes.
MAX_UNCONFIRMED_STARTS = 3
# Après ce délai, la confirmation est rejouée si la fenêtre principale a chargé
# (mark_window_loaded) ; une version vivante mais sans fenêtre n'est pas confirmée.
AUTO_CONFIRM_DELAY = 20.0
# Réglages du chien de garde (secondes).
INSTALLER_TIMING = {"poll": 1, "pid_wait": 60, "confirm_timeout": 300,
                    "grace": 30, "max_launches": 2}
_TOOLS = {
    "OPEN": "/usr/bin/open", "DITTO": "/usr/bin/ditto", "MV": "/bin/mv",
    "PGREP": "/usr/bin/pgrep", "PKILL": "/usr/bin/pkill", "XATTR": "/usr/bin/xattr",
    "LSREGISTER": ("/System/Library/Frameworks/CoreServices.framework/Frameworks/"
                   "LaunchServices.framework/Support/lsregister"),
}
_LSR = _TOOLS["LSREGISTER"]
_PART_MAX_AGE = 14 * 86400        # un .part plus vieux ne sera plus repris
_LOG_MAX = 512 * 1024

_sleep = time.sleep               # point d'injection des tests


# --------------------------------------------------------------------------- #
# Erreurs : code stable + phrase simple pour l'utilisateur + détail au journal.
# --------------------------------------------------------------------------- #
_MESSAGES = {
    "network": ("Connexion impossible pendant le téléchargement. Réessayez plus tard.",
                "Couldn't download the update. Check your connection and try again."),
    "stalled": ("Le téléchargement n'avance plus. Réessayez plus tard.",
                "The download stopped making progress. Try again later."),
    "no_space": ("Espace disque insuffisant pour la mise à jour. Libérez de la place puis réessayez.",
                 "Not enough disk space for the update. Free up some space and try again."),
    "size_mismatch": ("Le fichier téléchargé est incomplet. Réessayez.",
                      "The downloaded file is incomplete. Try again."),
    "checksum": ("Le fichier téléchargé est altéré. Réessayez.",
                 "The downloaded file is damaged. Try again."),
    "invalid_offer": ("La mise à jour proposée est invalide.",
                      "The offered update is invalid."),
    "mount_failed": ("Le fichier de mise à jour est illisible. Réessayez.",
                     "The update file can't be read. Try again."),
    "app_missing": ("Le fichier de mise à jour ne contient pas Vlocal.",
                    "The update file doesn't contain Vlocal."),
    "bad_bundle": ("Le contenu de la mise à jour est invalide : installation refusée.",
                   "The update content is invalid: installation refused."),
    "bad_signature": ("Signature de la mise à jour invalide : installation refusée.",
                      "The update's signature is invalid: installation refused."),
    "wrong_team": ("Mise à jour signée par un autre éditeur : installation refusée.",
                   "The update is signed by another publisher: installation refused."),
    "not_notarized": ("Mise à jour non notarisée par Apple : installation refusée.",
                      "The update isn't notarized by Apple: installation refused."),
    "wrong_arch": ("Cette mise à jour n'est pas compatible avec ce Mac.",
                   "This update isn't compatible with this Mac."),
    "downgrade": ("Cette version n'est pas plus récente que la vôtre.",
                  "This version isn't newer than yours."),
    "blocked_version": ("Cette version n'a pas démarré correctement sur ce Mac : Vlocal attend la suivante.",
                        "This version didn't start properly on this Mac: Vlocal will wait for the next one."),
    "stage_failed": ("Préparation de la mise à jour échouée. Réessayez.",
                     "Preparing the update failed. Try again."),
    "busy": ("Une mise à jour est déjà en cours.",
             "An update is already in progress."),
    "not_staged": ("Mise à jour non préparée (rien à installer).",
                   "The update isn't prepared (nothing to install)."),
    "no_permission": ("Vlocal ne peut pas se remplacer à cet emplacement. Téléchargez la mise à jour sur vlocal.org.",
                      "Vlocal can't replace itself at this location. Download the update from vlocal.org."),
    "launch_failed": ("L'installation n'a pas pu démarrer. Réessayez.",
                      "The installation couldn't start. Try again."),
    "failed": ("La mise à jour a échoué. Réessayez.",
               "The update failed. Try again."),
}


class UpdateError(Exception):
    """Échec de mise à jour. str(e) = phrase FR pour l'utilisateur (compatibilité
    avec app.py) ; e.code = code stable ; e.message(lang) = FR ou EN ;
    e.detail = détail technique (déjà écrit au journal par _fail)."""

    def __init__(self, code="failed", detail=""):
        if code not in _MESSAGES:
            code, detail = "failed", (detail or code)
        self.code = code
        self.detail = str(detail)
        self.message_fr, self.message_en = _MESSAGES[code]
        super().__init__(self.message_fr)

    def message(self, lang="fr"):
        return self.message_en if lang == "en" else self.message_fr


def _log(msg):
    print("[update] %s" % msg)


def _journal(code, **context):
    """Journal structuré (errors.py). Ne lève jamais."""
    try:
        import errors
        errors.log(code, **context)
    except Exception as e:  # noqa: BLE001
        _log("journal indisponible (%s) : %s %s" % (e, code, context))


def _fail(code, detail=""):
    """Écrit l'échec au journal et renvoie l'UpdateError à lever."""
    err = UpdateError(code, detail)
    _log("échec %s : %s" % (err.code, err.detail))
    _journal("update_fail", reason=err.code, detail=err.detail[:300])
    return err


def team_id_matches(codesign_output: str, expected: str = EXPECTED_TEAM_ID) -> bool:
    """True si la sortie de `codesign -dv --verbose=2` porte TeamIdentifier=expected."""
    for line in (codesign_output or "").splitlines():
        line = line.strip()
        if line.startswith("TeamIdentifier="):
            return line.split("=", 1)[1].strip() == expected
    return False


def _run(cmd, timeout=180):
    """Lance une commande système, renvoie (code, stdout, stderr). Jamais
    d'exception : un outil absent ou figé devient un code d'échec."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout or "", p.stderr or ""
    except Exception as e:  # noqa: BLE001
        return 255, "", "%s: %s" % (type(e).__name__, e)


# --------------------------------------------------------------------------- #
# Versions, état persistant, verrou.
# --------------------------------------------------------------------------- #
def parse_version(v):
    """'1.4.3' -> (1, 4, 3) ; None si ce n'est pas une version purement numérique
    (on refuse de comparer, donc d'installer, une version qu'on ne comprend pas)."""
    try:
        parts = tuple(int(x) for x in str(v).strip().split("."))
        return parts if parts and all(p >= 0 for p in parts) else None
    except (TypeError, ValueError):
        return None


def _cmp(a, b):
    """-1, 0, 1 (versions déjà parsées, complétées de zéros : 1.4 == 1.4.0)."""
    n = max(len(a), len(b))
    a, b = a + (0,) * (n - len(a)), b + (0,) * (n - len(b))
    return (a > b) - (a < b)


def _same(a, b):
    pa, pb = parse_version(a), parse_version(b)
    return pa is not None and pb is not None and _cmp(pa, pb) == 0


def _state_dir():
    return os.path.join(UPDATES, "state")


def _state(name):
    return os.path.join(_state_dir(), name)


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:  # noqa: BLE001
        _log("état illisible %s : %s" % (os.path.basename(path), e))
        return None


def _write_atomic(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _write_json(path, data):
    _write_atomic(path, json.dumps(data, ensure_ascii=False, sort_keys=True))


def _remove(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def _read_phase():
    try:
        with open(_state("phase"), encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""
    except OSError as e:
        _log("étape illisible : %s" % e)
        return ""


def _clear_pending():
    for name in ("pending.json", "phase", "confirmed"):
        _remove(_state(name))


def is_blocked(version):
    """True si cette version a déjà échoué à démarrer sur ce Mac (retour arrière) :
    elle ne doit plus être proposée, sinon l'app bouclerait sur la même panne."""
    blocked = _read_json(os.path.join(UPDATES, "blocked.json")) or []
    return any(_same(version, b) for b in blocked if isinstance(b, str))


def _block(version):
    path = os.path.join(UPDATES, "blocked.json")
    blocked = [b for b in (_read_json(path) or []) if isinstance(b, str)]
    if version and not any(_same(version, b) for b in blocked):
        blocked.append(version)
        _write_json(path, blocked)


def _installer_alive():
    """True si un installateur tient son verrou (pid vivant)."""
    try:
        with open(os.path.join(_state("installer.lock"), "pid")) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except (OSError, ValueError):
        return False


class _UpdateLock:
    """Verrou exclusif inter-processus (flock) sur toute étape qui écrit dans la
    zone de mise à jour. Non bloquant : un second demandeur reçoit « busy ».
    `quiet` : le nettoyage de fond renonce sans rien écrire au journal, car ce
    n'est pas un échec."""

    def __init__(self, quiet=False):
        self._quiet = quiet

    def _busy(self, detail):
        return UpdateError("busy", detail) if self._quiet else _fail("busy", detail)

    def __enter__(self):
        import fcntl
        os.makedirs(UPDATES, exist_ok=True)
        self._f = open(os.path.join(UPDATES, "update.lock"), "a")
        try:
            fcntl.flock(self._f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._f.close()
            raise self._busy("verrou update.lock tenu") from exc
        if _installer_alive():
            self._f.close()
            raise self._busy("installateur en cours")
        return self

    def __exit__(self, *exc):
        self._f.close()          # fermer relâche le flock
        return False


# --------------------------------------------------------------------------- #
# 1) TÉLÉCHARGEMENT
# --------------------------------------------------------------------------- #
_DL_CODES = {"network": "network", "http": "network", "stalled": "stalled",
             "no_space": "no_space", "size_mismatch": "size_mismatch",
             "checksum": "checksum"}


def _dmg_path(version):
    return os.path.join(UPDATES, "Vlocal-%s.dmg" % version)


def download(url, expected_len, version, on_progress=None, sha256=None):
    """Télécharge le DMG de la MAJ vers UPDATES et renvoie son chemin.

    `expected_len` : taille annoncée par get-latest-version (0 si inconnue).
    `sha256` : empreinte optionnelle (champ `sha256` de get-latest-version) ;
    sans elle, la taille exacte et la signature vérifiée ensuite font foi.
    Espace vérifié AVANT : DMG + sa copie décompressée (×2) + marge.
    on_progress(frac 0..1, label). Lève UpdateError."""
    if parse_version(version) is None:
        raise _fail("invalid_offer", "version %r" % (version,))
    if not str(url or "").startswith("https://"):
        raise _fail("invalid_offer", "url non https")
    if is_blocked(version):
        raise _fail("blocked_version", version)
    total = int(expected_len or 0) or None
    dst = _dmg_path(version)
    with _UpdateLock():
        if (total or sha256) and model_store.file_matches(dst, total, sha256):
            if on_progress:
                on_progress(1.0, "Téléchargement terminé")
            return dst                       # déjà téléchargé et intact
        _remove(dst)
        part = dst + ".part"
        try:
            if total:
                have = os.path.getsize(part) if os.path.exists(part) else 0
                model_store.ensure_free_space(UPDATES, 2 * total - have)

            def _bytes(n):
                if on_progress and total:
                    on_progress(min(n / total, 1.0), "Téléchargement")
            model_store.fetch_resumable(url, dst, size=total, sha256=sha256, on_bytes=_bytes)
        except model_store.DownloadError as e:
            raise _fail(_DL_CODES.get(e.code, "failed"), e.detail or e.code) from e
        if on_progress:
            on_progress(1.0, "Téléchargement terminé")
        return dst


# --------------------------------------------------------------------------- #
# 2) VÉRIFICATION + STAGING
# --------------------------------------------------------------------------- #
_CPU_NAMES = {0x0100000C: "arm64", 0x01000007: "x86_64", 7: "i386", 12: "arm"}


def macho_archs(path):
    """Architectures d'un exécutable Mach-O (fin ou universel), lues dans l'en-tête :
    pas besoin de `lipo`, absent sans les outils de développement."""
    archs = set()
    with open(path, "rb") as f:
        head = f.read(8)
        if len(head) < 8:
            return archs
        be = struct.unpack(">I", head[:4])[0]
        le = struct.unpack("<I", head[:4])[0]
        if be in (0xCAFEBABE, 0xCAFEBABF):              # universel (fat / fat64)
            n = struct.unpack(">I", head[4:8])[0]
            if n > 16:
                return archs                             # en-tête incohérent
            size = 20 if be == 0xCAFEBABE else 32
            for _ in range(n):
                entry = f.read(size)
                if len(entry) < 4:
                    break
                archs.add(_CPU_NAMES.get(struct.unpack(">I", entry[:4])[0], "?"))
        elif le in (0xFEEDFACF, 0xFEEDFACE):
            archs.add(_CPU_NAMES.get(struct.unpack("<I", head[4:8])[0], "?"))
        elif be in (0xFEEDFACF, 0xFEEDFACE):
            archs.add(_CPU_NAMES.get(struct.unpack(">I", head[4:8])[0], "?"))
    return archs


def read_bundle_info(app):
    """Info.plist d'un .app (dict), ou None s'il est absent ou illisible."""
    try:
        with open(os.path.join(app, "Contents", "Info.plist"), "rb") as f:
            info = plistlib.load(f)
        return info if isinstance(info, dict) else None
    except Exception:  # noqa: BLE001 — absent, tronqué, binaire invalide : même verdict
        return None


def _check_bundle(app, expected_version, current_version):
    """Contrôles rapides AVANT les signatures : identité, version, architecture.
    Renvoie la version du bundle. Lève UpdateError."""
    info = read_bundle_info(app)
    if info is None:
        raise _fail("bad_bundle", "Info.plist illisible")
    if info.get("CFBundleIdentifier") != BUNDLE_ID:
        raise _fail("bad_bundle", "identifiant %r" % info.get("CFBundleIdentifier"))
    version = str(info.get("CFBundleShortVersionString") or "")
    pv = parse_version(version)
    if pv is None:
        raise _fail("bad_bundle", "version %r" % version)
    if expected_version and not _same(version, expected_version):
        raise _fail("bad_bundle", "version %s annoncée, %s trouvée" % (expected_version, version))
    pc = parse_version(current_version) if current_version else None
    if pc is not None and _cmp(pv, pc) <= 0:
        raise _fail("downgrade", "%s <= %s" % (version, current_version))
    if is_blocked(version):
        raise _fail("blocked_version", version)
    exe = os.path.join(app, "Contents", "MacOS", str(info.get("CFBundleExecutable") or ""))
    try:
        archs = macho_archs(exe)
    except OSError as e:
        raise _fail("bad_bundle", "exécutable illisible : %s" % e) from e
    if "arm64" not in archs:
        raise _fail("wrong_arch", "architectures %s" % sorted(archs))
    return version


def _check_signatures(app):
    """Signature stricte, éditeur épinglé, notarisation Apple. Lève UpdateError."""
    code, out, err = _run(["/usr/bin/codesign", "--verify", "--deep", "--strict",
                           "--verbose=2", app], timeout=300)
    if code != 0:
        raise _fail("bad_signature", (out + err).strip()[-300:])
    code, out, err = _run(["/usr/bin/codesign", "-dv", "--verbose=2", app], timeout=60)
    if code != 0 or not team_id_matches(out + err):
        raise _fail("wrong_team", (out + err).strip()[-300:])
    # spctl accepte aussi un Developer ID non notarisé dans certaines politiques :
    # on exige la source « Notarized Developer ID » (vraie hors ligne grâce au
    # ticket agrafé).
    code, out, err = _run(["/usr/sbin/spctl", "--assess", "--type", "exec",
                           "--verbose=2", app], timeout=300)
    if code != 0 or "source=Notarized Developer ID" not in (out + err):
        raise _fail("not_notarized", (out + err).strip()[-300:])


def normalize_modes(root):
    """Ramène chaque fichier du bundle à 644 (755 s'il est exécutable) et chaque
    dossier à 755. Des fichiers en 640 empêchaient un AUTRE compte du Mac de
    charger les bibliothèques Metal (GPU) de l'app. Les modes ne font pas partie
    du sceau de signature. Les liens symboliques ne sont pas suivis. Renvoie le
    nombre d'entrées corrigées."""
    fixed = 0
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            p = os.path.join(dirpath, name)
            st = os.lstat(p)
            if os.path.islink(p):
                continue
            mode = st.st_mode & 0o7777
            if os.path.isdir(p) or (mode & 0o111):
                want = 0o755
            else:
                want = 0o644
            if mode != want:
                os.chmod(p, want)
                fixed += 1
    if not os.path.islink(root) and (os.stat(root).st_mode & 0o7777) != 0o755:
        os.chmod(root, 0o755)
        fixed += 1
    return fixed


def _tree_size(root):
    total = 0
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                pass
    return total


def _existing(path):
    while not os.path.exists(path) and os.path.dirname(path) != path:
        path = os.path.dirname(path)
    return path


def _ensure_install_space(app_bytes, target):
    """La copie de staging (UPDATES) et la copie entrante (à côté de l'app)
    doivent tenir ; ×2 si elles partagent le volume."""
    try:
        same = os.stat(_existing(UPDATES)).st_dev == os.stat(_existing(target)).st_dev
        if same:
            model_store.ensure_free_space(UPDATES, 2 * app_bytes)
        else:
            model_store.ensure_free_space(UPDATES, app_bytes)
            model_store.ensure_free_space(os.path.dirname(target), app_bytes)
    except model_store.DownloadError as e:
        raise _fail("no_space", e.detail) from e


def _rmtree(path):
    """Supprime un dossier qui nous appartient. Lève si la suppression échoue."""
    import shutil
    if os.path.islink(path):
        os.remove(path)
    elif os.path.isdir(path):
        shutil.rmtree(path)


def _attach(dmg_path):
    """Monte le DMG en lecture seule, sans Finder, sous UPDATES/mnt.
    Renvoie (point de montage, périphériques). Lève UpdateError."""
    mnt_root = os.path.join(UPDATES, "mnt")
    os.makedirs(mnt_root, exist_ok=True)
    code, out, err = _run(["/usr/bin/hdiutil", "attach", "-nobrowse", "-noautoopen",
                           "-readonly", "-mountrandom", mnt_root, "-plist", dmg_path],
                          timeout=300)
    mount, devs = None, []
    try:
        for ent in plistlib.loads(out.encode("utf-8")).get("system-entities", []):
            if ent.get("dev-entry"):
                devs.append(ent["dev-entry"])
            if ent.get("mount-point") and not mount:
                mount = ent["mount-point"]
    except Exception as e:  # noqa: BLE001
        if code == 0:
            _log("sortie de hdiutil illisible (%s) : démontage de sécurité" % e)
            _detach_orphans()
            raise _fail("mount_failed", "plist hdiutil illisible") from e
    if code != 0:
        _detach(mount, devs)
        raise _fail("mount_failed", err.strip()[-300:])
    if not mount or not os.path.isdir(mount):
        _detach(mount, devs)
        raise _fail("mount_failed", "aucun point de montage")
    return mount, devs


def _detach(mount, devs):
    """Démonte, en insistant (3 essais, le dernier forcé). Ne lève jamais : un
    échec est écrit au journal et rattrapé par _detach_orphans au lancement."""
    target = sorted(devs, key=len)[0] if devs else mount
    if not target:
        return True
    for i in range(3):
        cmd = ["/usr/bin/hdiutil", "detach", target] + (["-force"] if i == 2 else [])
        code, _out, err = _run(cmd, timeout=60)
        if code == 0:
            break
        _log("démontage %s refusé (%s), essai %d/3" % (target, err.strip()[-120:], i + 1))
        _sleep(1)
    still = bool(mount) and os.path.ismount(mount)
    if still:
        _journal("update_fail", reason="detach", detail=str(mount))
    elif mount:
        try:
            os.rmdir(mount)
        except OSError:
            pass
    return not still


def _detach_orphans():
    """Démonte tout volume resté monté sous UPDATES/mnt (app tuée pendant une
    vérification). Seul cet endroit est examiné : on ne touche aucun autre DMG."""
    mnt_root = os.path.join(UPDATES, "mnt")
    if not os.path.isdir(mnt_root):
        return
    for name in os.listdir(mnt_root):
        p = os.path.join(mnt_root, name)
        if os.path.ismount(p):
            _log("volume orphelin démonté : %s" % p)
            _detach(p, [])
        elif os.path.isdir(p):
            try:
                os.rmdir(p)
            except OSError:
                pass


def _version_from_dmg(dmg_path):
    name = os.path.basename(dmg_path)
    if name.startswith("Vlocal-") and name.endswith(".dmg"):
        v = name[len("Vlocal-"):-len(".dmg")]
        return v if parse_version(v) else None
    return None


def verify_and_stage(dmg_path, on_stage=None, expected_version=None, current_version=None):
    """Monte le DMG, vérifie l'app (identité, version > version courante, arm64,
    signature stricte, Team ID, notarisation), la copie dans UPDATES/Vlocal.app,
    démonte, normalise modes et quarantaine puis revérifie la signature de la
    copie. Renvoie ce chemin. Lève UpdateError (rien n'est installé).
    `expected_version` par défaut : celle du nom du DMG ; `current_version` par
    défaut : celle du bundle en cours d'exécution."""
    expected_version = expected_version or _version_from_dmg(dmg_path)
    current_version = current_version or _running_version()
    staged = os.path.join(UPDATES, APP_NAME)
    with _UpdateLock():
        if on_stage:
            on_stage("verify")
        mount, devs = _attach(dmg_path)
        try:
            app = os.path.join(mount, APP_NAME)
            if not os.path.isdir(app):
                raise _fail("app_missing", mount)
            version = _check_bundle(app, expected_version, current_version)
            _check_signatures(app)
            if on_stage:
                on_stage("stage")
            _ensure_install_space(_tree_size(app), resolve_target())
            _rmtree(staged)
            code, _out, err = _run(["/usr/bin/ditto", app, staged], timeout=900)
            if code != 0 or not os.path.isdir(staged):
                raise _fail("stage_failed", "ditto : %s" % err.strip()[-300:])
        except UpdateError:
            raise
        except Exception as e:  # noqa: BLE001
            raise _fail("stage_failed", "%s: %s" % (type(e).__name__, e)) from e
        finally:
            _detach(mount, devs)
        try:
            code, _out, err = _run(["/usr/bin/xattr", "-dr", "com.apple.quarantine", staged],
                                   timeout=120)
            if code != 0:
                _log("quarantaine non retirée de la copie : %s" % err.strip()[-200:])
            fixed = normalize_modes(staged)
            if fixed:
                _log("%d droits d'accès normalisés (644/755)" % fixed)
            code, out, err = _run(["/usr/bin/codesign", "--verify", "--deep", "--strict",
                                   staged], timeout=300)
            if code != 0:
                raise _fail("stage_failed", "copie non conforme : %s" % (out + err).strip()[-300:])
        except Exception as e:
            try:
                _rmtree(staged)
            except OSError as rm_err:
                _log("copie défectueuse non supprimée : %s" % rm_err)
            if isinstance(e, UpdateError):
                raise
            raise _fail("stage_failed", "%s: %s" % (type(e).__name__, e)) from e
        _log("version %s vérifiée et prête : %s" % (version, staged))
        return staged


# --------------------------------------------------------------------------- #
# 3) INSTALLATION — script détaché qui SURVIT à la mort de l'app : il attend que
#    le process meure, copie à côté, échange en deux renommages, relance, puis
#    surveille le démarrage et revient à l'ancienne version sans confirmation.
# --------------------------------------------------------------------------- #
_INSTALLER = r'''#!/bin/sh
# Vlocal — installateur de mise à jour, généré par updater.render_installer().
# Détaché de l'app (nouvelle session) : il survit à sa fermeture. Garanties :
#   - une seule installation à la fois (verrou par mkdir, atomique) ;
#   - l'app en place n'est jamais détruite avant que la nouvelle soit posée :
#     copie à côté, puis deux renommages sur le même volume, chacun annulé si le
#     suivant échoue ; s'il manque l'app, la sauvegarde reprend sa place ;
#   - l'ancienne version reste en sauvegarde jusqu'à ce que la nouvelle confirme
#     son démarrage ; sans confirmation, retour arrière automatique.
set -u
PATH=/usr/bin:/bin:/usr/sbin:/sbin; export PATH
MODE=@MODE@
PID=@PID@
STAGED=@STAGED@
TARGET=@TARGET@
STATE=@STATE@
LOG=@LOG@
OPEN=@OPEN@
DITTO=@DITTO@
MV=@MV@
PGREP=@PGREP@
PKILL=@PKILL@
LSREGISTER=@LSREGISTER@
XATTR=@XATTR@
POLL=@POLL@
PID_WAIT_TICKS=@PID_WAIT_TICKS@
CONFIRM_TICKS=@CONFIRM_TICKS@
GRACE_TICKS=@GRACE_TICKS@
MAX_LAUNCHES=@MAX_LAUNCHES@

DIR=$(dirname "$TARGET")
NAME=$(basename "$TARGET" .app)
INCOMING="$DIR/.$NAME-incoming.app"
BAK="$DIR/.$NAME-backup.app"
FAILED="$DIR/.$NAME-failed.app"
EXE="$TARGET/Contents/MacOS/"
LOCK="$STATE/installer.lock"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >>"$LOG" 2>/dev/null; }
phase() {
  printf '%s\n' "$1" >"$STATE/phase.tmp" 2>/dev/null && mv -f "$STATE/phase.tmp" "$STATE/phase"
  log "étape : $1"
}
reopen() { "$OPEN" -n "$TARGET" >/dev/null 2>&1 || log "ouverture de $TARGET impossible"; }
running() { "$PGREP" -f "$EXE" >/dev/null 2>&1; }

# L'app doit toujours exister : une installation coupée entre les deux
# renommages est réparée par la sauvegarde (la version qui marchait).
ensure_target() {
  [ -d "$TARGET" ] && return 0
  for src in "$BAK" "$FAILED"; do
    if [ -d "$src" ] && "$MV" "$src" "$TARGET"; then log "réparation : $src remis en place"; return 0; fi
  done
  log "aucune app à remettre en place"
  return 1
}

wait_pid() {
  [ -n "$PID" ] || return 0
  i=0
  while kill -0 "$PID" 2>/dev/null; do
    [ "$i" -ge "$PID_WAIT_TICKS" ] && return 1
    sleep "$POLL"; i=$((i+1))
  done
  return 0
}

stop_new_version() {
  "$PKILL" -f "$EXE" >/dev/null 2>&1
  i=0
  while running && [ "$i" -lt 10 ]; do sleep "$POLL"; i=$((i+1)); done
  running && "$PKILL" -9 -f "$EXE" >/dev/null 2>&1
  return 0
}

rollback() {
  log "retour arrière : $1"
  stop_new_version
  if [ ! -d "$BAK" ]; then
    phase "failed:no_backup"; ensure_target; reopen; return 1
  fi
  rm -rf "$FAILED"
  if "$MV" "$TARGET" "$FAILED"; then
    if "$MV" "$BAK" "$TARGET"; then
      rm -rf "$FAILED"; phase "rolled_back"
    else
      "$MV" "$FAILED" "$TARGET"; phase "failed:rollback"
    fi
  else
    phase "failed:rollback"
  fi
  ensure_target
  "$LSREGISTER" -f "$TARGET" >/dev/null 2>&1
  reopen
}

take_lock() {
  if mkdir "$LOCK" 2>/dev/null; then echo $$ >"$LOCK/pid"; return 0; fi
  other=$(cat "$LOCK/pid" 2>/dev/null || true)
  if [ -n "$other" ] && kill -0 "$other" 2>/dev/null; then return 1; fi
  rm -rf "$LOCK"
  mkdir "$LOCK" 2>/dev/null || return 1
  echo $$ >"$LOCK/pid"
}

mkdir -p "$STATE"
if ! take_lock; then log "une autre installation est en cours : abandon"; exit 3; fi
trap 'rm -rf "$LOCK"' EXIT
log "début ($MODE) : $TARGET"
ensure_target

if [ "$MODE" = "rollback" ]; then
  if ! wait_pid; then
    kill -TERM "$PID" 2>/dev/null
    wait_pid || kill -KILL "$PID" 2>/dev/null
  fi
  rollback "démarrage jamais confirmé"
  exit 2
fi

if ! wait_pid; then phase "failed:still_running"; exit 1; fi

phase "copying"
rm -rf "$INCOMING" "$FAILED"
[ -d "$TARGET" ] && rm -rf "$BAK"
if ! "$DITTO" "$STAGED" "$INCOMING"; then
  rm -rf "$INCOMING"; phase "failed:copy"; reopen; exit 1
fi
phase "swapping"
if [ -d "$TARGET" ] && ! "$MV" "$TARGET" "$BAK"; then
  rm -rf "$INCOMING"; phase "failed:permission"; reopen; exit 1
fi
if ! "$MV" "$INCOMING" "$TARGET"; then
  ensure_target; rm -rf "$INCOMING"; phase "failed:swap"; reopen; exit 1
fi
phase "swapped"
"$XATTR" -dr com.apple.quarantine "$TARGET" >/dev/null 2>&1
"$LSREGISTER" -f "$TARGET" >/dev/null 2>&1
rm -f "$STATE/confirmed"
reopen
launches=1; ticks=0; since=0
while [ "$ticks" -lt "$CONFIRM_TICKS" ]; do
  if [ -f "$STATE/confirmed" ]; then
    rm -rf "$BAK" "$STAGED"; phase "confirmed"; exit 0
  fi
  if [ "$since" -ge "$GRACE_TICKS" ] && ! running; then
    [ "$launches" -ge "$MAX_LAUNCHES" ] && break
    launches=$((launches+1)); since=0
    log "la nouvelle version s'est arrêtée sans confirmer : relance $launches/$MAX_LAUNCHES"
    reopen
  fi
  sleep "$POLL"; ticks=$((ticks+1)); since=$((since+1))
done
if [ -f "$STATE/confirmed" ]; then
  rm -rf "$BAK" "$STAGED"; phase "confirmed"; exit 0
fi
rollback "démarrage non confirmé"
exit 2
'''


def _ticks(seconds, poll):
    return max(1, int(-(-float(seconds) // float(poll))))


def render_installer(mode, pid, staged, target, tools=None, timing=None):
    """Script de l'installateur, toutes valeurs échappées pour le shell.
    `tools` / `timing` remplacent les outils et délais (bancs d'essai)."""
    t = dict(_TOOLS, **(tools or {}))
    tm = dict(INSTALLER_TIMING, **(timing or {}))
    values = dict(t)
    values.update({
        "MODE": mode, "PID": "" if pid is None else str(int(pid)),
        "STAGED": staged or "", "TARGET": target, "STATE": _state_dir(),
        "LOG": os.path.join(UPDATES, "install.log"), "POLL": str(tm["poll"]),
        "PID_WAIT_TICKS": str(_ticks(tm["pid_wait"], tm["poll"])),
        "CONFIRM_TICKS": str(_ticks(tm["confirm_timeout"], tm["poll"])),
        "GRACE_TICKS": str(_ticks(tm["grace"], tm["poll"])),
        "MAX_LAUNCHES": str(int(tm["max_launches"])),
    })
    script = _INSTALLER
    for key, val in values.items():
        script = script.replace("@%s@" % key, shlex.quote(str(val)))
    return script


def _spawn_installer(mode, pid, staged, target):
    """Écrit l'installateur et le lance DÉTACHÉ (nouvelle session : survit à la
    fermeture de l'app). Lève OSError si le lancement échoue."""
    os.makedirs(_state_dir(), exist_ok=True)
    log = os.path.join(UPDATES, "install.log")
    try:
        if os.path.getsize(log) > _LOG_MAX:
            os.replace(log, log + ".1")
    except OSError:
        pass
    sh = os.path.join(UPDATES, "install.sh")
    _write_atomic(sh, render_installer(mode, pid, staged, target))
    os.chmod(sh, 0o700)
    subprocess.Popen(["/bin/sh", sh], start_new_session=True, cwd="/", close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)


def _self_bundle():
    """Chemin du .app qui EXÉCUTE ce process (PyInstaller : sys.executable =
    .../Vlocal.app/Contents/MacOS/Vlocal). None hors d'un bundle (mode source)."""
    try:
        p = os.path.realpath(sys.executable)
    except Exception:  # noqa: BLE001
        return None
    marker = ".app/Contents/MacOS/"
    i = p.find(marker)
    return p[:i + 4] if i != -1 else None


def _running_version():
    b = _self_bundle()
    info = read_bundle_info(b) if b else None
    return str(info.get("CFBundleShortVersionString")) if info else None


def resolve_target(bundle=None):
    """Où installer. Le bundle en cours s'il est dans un dossier où l'on peut
    écrire (ex. Téléchargements) ; /Applications/Vlocal.app s'il tourne depuis
    un DMG, un volume en lecture seule ou la translocation de Gatekeeper (copie
    temporaire, aléatoire : la remplacer n'aurait aucun effet durable)."""
    b = bundle if bundle is not None else _self_bundle()
    if b:
        real = os.path.realpath(b)
        if "/AppTranslocation/" not in real and os.access(os.path.dirname(real), os.W_OK):
            return real
    return TARGET


def _check_target_writable(target):
    parent = os.path.dirname(target)
    if not os.path.isdir(parent) or not os.access(parent, os.W_OK):
        raise _fail("no_permission", "dossier non inscriptible : %s" % parent)
    if os.path.exists(target) and not os.access(target, os.W_OK):
        raise _fail("no_permission", "app non inscriptible : %s" % target)


def _siblings(target):
    d, name = os.path.dirname(target), os.path.basename(target)[:-len(".app")]
    return {k: os.path.join(d, ".%s-%s.app" % (name, k))
            for k in ("incoming", "backup", "failed")}


def apply_update(staged_path, app_pid, to_version=None, from_version=None):
    """Lance l'installateur détaché puis rend la main : app.py affiche « Vlocal
    va se fermer » et termine le process. L'installateur attend sa mort, pose la
    nouvelle version, la relance et la surveille. Renvoie True. Lève UpdateError."""
    if not staged_path or not os.path.isdir(staged_path):
        raise _fail("not_staged", str(staged_path))
    info = read_bundle_info(staged_path)
    to_version = to_version or (info or {}).get("CFBundleShortVersionString")
    if parse_version(to_version) is None:
        raise _fail("bad_bundle", "version préparée %r" % (to_version,))
    target = resolve_target()
    _check_target_writable(target)
    with _UpdateLock():
        _clear_pending()
        _write_json(_state("pending.json"), {
            "to_version": str(to_version),
            "from_version": from_version or _running_version(),
            "target": target, "staged": staged_path,
            "created_at": time.time(), "attempts": 0,
        })
        try:
            _spawn_installer("install", app_pid, staged_path, target)
        except OSError as e:
            _clear_pending()
            raise _fail("launch_failed", e) from e
    _log("installateur lancé : %s -> %s" % (to_version, target))
    return True


# --------------------------------------------------------------------------- #
# 4) DÉMARRAGE — confirmation, retour arrière, nettoyage.
# --------------------------------------------------------------------------- #
def _in_background(fn):
    """Exécute `fn` dans un thread démon : le lancement de l'UI n'attend jamais."""
    threading.Thread(target=fn, daemon=True).start()


# Posé par la fenêtre principale quand elle a réellement chargé
# (startup.on_main_window_loaded -> mark_window_loaded).
_window_loaded = threading.Event()


def mark_window_loaded():
    """La fenêtre principale a chargé : seule preuve qu'un démarrage a réussi."""
    _window_loaded.set()


def _auto_confirm(version):
    """Filet de la confirmation : rejoue confirm_startup si la fenêtre a chargé.
    Sans fenêtre chargée, ne confirme rien : le démarrage reste non confirmé et
    le retour arrière peut jouer au démarrage suivant."""
    if not _window_loaded.is_set():
        _log("fenêtre non chargée après %ds : version %s non confirmée"
             % (AUTO_CONFIRM_DELAY, version))
        return False
    return confirm_startup(version)


def _schedule_auto_confirm(version):
    t = threading.Timer(AUTO_CONFIRM_DELAY, _auto_confirm, args=(version,))
    t.daemon = True
    t.start()


def confirm_startup(version=None):
    """À appeler par la nouvelle version dès que sa fenêtre est affichée : le
    démarrage est réussi, la sauvegarde de l'ancienne version peut partir.
    Idempotent. Renvoie True si une mise à jour en attente vient d'être confirmée."""
    try:
        running = version or _running_version()
        pending = _read_json(_state("pending.json"))
        if not pending or not running or not _same(running, pending.get("to_version")):
            return False
        _write_atomic(_state("confirmed"), str(running))
        _log("démarrage de la version %s confirmé" % running)
        if not _installer_alive():
            _clear_pending()
            _in_background(_purge)
        return True
    except Exception as e:  # noqa: BLE001
        _log("confirmation impossible : %s" % e)
        _journal("update_fail", reason="confirm", detail=str(e)[:300])
        return False


def _startup_transition(running):
    """Machine à états du démarrage. Renvoie un rapport {status, version, ...}.

    idle            aucune mise à jour en attente
    confirming      nouvelle version lancée, confirmation attendue
    updated         nouvelle version déjà confirmée
    rollback_started trop de démarrages sans confirmation : retour arrière lancé,
                    l'app doit se fermer (l'installateur la remplace et relance)
    rolled_back     l'ancienne version tourne après un retour arrière : la version
                    fautive est bloquée, ne sera plus proposée
    installing      un installateur travaille encore : on ne touche à rien
    failed          l'installation n'a pas abouti : l'app en place est intacte"""
    pending = _read_json(_state("pending.json"))
    if not isinstance(pending, dict) or not running:
        return {"status": "idle"}
    to = pending.get("to_version")
    phase = _read_phase()
    alive = _installer_alive()
    target = pending.get("target") or TARGET
    if _same(running, to):
        if phase == "confirmed" or os.path.exists(_state("confirmed")):
            _clear_pending()
            return {"status": "updated", "version": to}
        pending["attempts"] = int(pending.get("attempts") or 0) + 1
        if not alive and pending["attempts"] > MAX_UNCONFIRMED_STARTS:
            if pending.get("rollback_requested") or not os.path.isdir(_siblings(target)["backup"]):
                # Retour arrière déjà tenté ou sans sauvegarde : on garde la
                # version qui tourne plutôt que de boucler.
                _clear_pending()
                _journal("update_fail", reason="rollback_impossible", detail=str(to))
                return {"status": "updated", "version": to, "unconfirmed": True}
            pending["rollback_requested"] = True
            _write_json(_state("pending.json"), pending)
            _spawn_installer("rollback", os.getpid(), "", target)
            _journal("update_fail", reason="rollback_started", detail=str(to))
            return {"status": "rollback_started", "version": to}
        _write_json(_state("pending.json"), pending)
        _schedule_auto_confirm(to)
        return {"status": "confirming", "version": to, "attempts": pending["attempts"]}
    if phase == "rolled_back":
        _block(to)
        _clear_pending()
        _journal("update_fail", reason="rolled_back", detail=str(to))
        return {"status": "rolled_back", "version": to}
    if alive:
        return {"status": "installing", "version": to}
    _clear_pending()
    reason = phase[len("failed:"):] if phase.startswith("failed:") else (phase or "interrupted")
    _journal("update_fail", reason="install_" + reason, detail=str(to))
    return {"status": "failed", "version": to, "reason": reason}


def _purge(current_version=None):
    """Supprime NOS restes, désignés nommément, jamais autre chose :
    copies entrante/échouée à côté de l'app, sauvegarde si aucune mise à jour
    n'attend de confirmation, copie de staging, DMG et .part des versions déjà
    installées (ou trop vieux pour être repris), script d'installation."""
    try:
        with _UpdateLock(quiet=True):
            _purge_locked(current_version)
    except UpdateError:
        _log("nettoyage reporté : une mise à jour est en cours")


def _purge_locked(current_version):
    pending = _read_json(_state("pending.json"))
    targets = {resolve_target()}
    if isinstance(pending, dict) and pending.get("target"):
        targets.add(pending["target"])
    victims = []
    for target in targets:
        sib = _siblings(target)
        victims += [sib["incoming"], sib["failed"]]
        if not pending:
            victims.append(sib["backup"])
    if not pending:
        victims += [os.path.join(UPDATES, APP_NAME), os.path.join(UPDATES, "install.sh")]
    current = parse_version(current_version) if current_version else None
    if os.path.isdir(UPDATES):
        now = time.time()
        for name in os.listdir(UPDATES):
            if not name.startswith("Vlocal-") or not (name.endswith(".dmg") or name.endswith(".dmg.part")):
                continue
            path = os.path.join(UPDATES, name)
            v = parse_version(name[len("Vlocal-"):].split(".dmg")[0])
            installed = v is not None and current is not None and _cmp(v, current) <= 0
            try:
                stale = now - os.path.getmtime(path) > _PART_MAX_AGE
            except OSError:
                continue
            if v is None or installed or stale:
                victims.append(path)
    for path in victims:
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                _rmtree(path)
                _log("reste supprimé : %s" % path)
            elif os.path.lexists(path):
                os.remove(path)
        except OSError as e:
            _log("reste non supprimé %s : %s" % (path, e))


def cleanup_leftovers(current_version=None):
    """À appeler au lancement. Décide du sort d'une mise à jour en attente
    (confirmation, retour arrière, blocage), démonte un DMG orphelin, puis
    nettoie nos restes et dédoublonne les icônes en tâche de fond.

    Renvoie le rapport de _startup_transition. Si status == 'rollback_started',
    l'app doit se fermer : l'installateur attend sa mort pour remettre
    l'ancienne version et la relancer. Ne lève jamais."""
    running = current_version or _running_version()
    report = {"status": "idle"}
    try:
        report = _startup_transition(running)
    except Exception as e:  # noqa: BLE001
        _log("état de mise à jour illisible : %s" % e)
        _journal("update_fail", reason="startup_state", detail=str(e)[:300])
    if report.get("status") != "idle":
        _log("au démarrage : %s" % report)
    try:
        _detach_orphans()
    except Exception as e:  # noqa: BLE001
        _log("démontage des orphelins impossible : %s" % e)

    def _background():
        for step in (lambda: _purge(running), _dedupe_icons):
            try:
                step()
            except Exception as e:  # noqa: BLE001
                _log("nettoyage de fond : %s" % e)
    try:
        _in_background(_background)
    except Exception as e:  # noqa: BLE001
        _log("nettoyage de fond non lancé : %s" % e)
    return report


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
    code, out, _err = _run([_LSR, "-dump"], timeout=90)
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
        # WebKit com.vlocal.app, ni la sauvegarde .Vlocal-backup.app).
        if os.path.basename(m) != APP_NAME or m in seen:
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
