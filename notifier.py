#!/usr/bin/env python3
"""
Vlocal — Notifications natives macOS, marque "Vlocal" (bundle .app dédié).

v15 — Refonte branding :
  - AVANT : `osascript display notification` → la notif s'affichait sous
    l'identité de "Script Editor" (parfois "TextEdit"), avec son icône. Pas
    premium du tout.
  - MAINTENANT : on construit UNE FOIS un petit bundle `VlocalNotify.app`
    (AppleScript compilé) avec CFBundleName="Vlocal" et une icône V premium
    (V blanc sur carré charbon arrondi). Les notifications passent par ce
    bundle → elles s'affichent sous le nom "Vlocal" avec NOTRE icône.
  - REPLI ROBUSTE : si la construction du bundle échoue (outils manquants,
    permissions…), on retombe automatiquement sur `osascript`. Les
    notifications NE CASSENT JAMAIS — au pire elles perdent juste le branding.

Construction du bundle (idempotente, mise en cache dans App Support) :
  1. icône premium PNG via pyobjc off-screen (NSBitmapImageRep, sans NSWindow)
  2. .icns via iconutil
  3. applet via osacompile (lit un fichier payload puis display notification)
  4. Info.plist : CFBundleName=Vlocal, identifiant, LSUIElement, icône
  5. signature ad-hoc

Première notif : macOS demandera l'autorisation pour "Vlocal" (Réglages
Système → Notifications). C'est voulu : c'est ce qui donne le branding.

Planification : assurée par app.py (_schedule_reminder, re-armée au démarrage
via _rearm_pending_reminders) ; ce module fournit l'affichage immédiat (show),
parse_iso et le pré-build du bundle.

Le mode brut (dictée → presse-papier) ne dépend JAMAIS de ce module.
"""

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

DRY_RUN = os.environ.get("VLOCAL_NOTIF_DRY", "0") == "1"
_IS_MAC = sys.platform == "darwin"

# v16 — frozen-aware (dev = dossier script ; .app empaqueté = _MEIPASS)
if getattr(sys, "frozen", False):
    BASE_DIR = Path(getattr(sys, "_MEIPASS",
                            os.path.dirname(os.path.abspath(__file__))))
else:
    BASE_DIR = Path(os.path.dirname(os.path.abspath(__file__)))
ASSETS_DIR = BASE_DIR / "assets"
APP_SUPPORT = Path(os.path.expanduser("~/Library/Application Support/Vlocal"))
BUNDLE_PATH = APP_SUPPORT / "VlocalNotify.app"
# Marqueur de version : bump pour forcer une reconstruction du bundle.
_BUNDLE_VERSION = "1"
_BUNDLE_MARKER = APP_SUPPORT / ".notify_bundle_v"

# Résolu paresseusement au premier show() : None = pas encore tenté,
# True = bundle prêt, False = indisponible (on reste sur osascript).
_bundle_ready: Optional[bool] = None
_bundle_lock = threading.Lock()


def _sanitize(s: str, maxlen: int = 240) -> str:
    """Nettoie un champ de notif : retire les caractères de contrôle / retours
    ligne (qui casseraient le payload ligne-à-ligne du bundle ou l'AppleScript),
    borne la longueur. Défense contre une dictée contenant n'importe quoi."""
    s = (s or "")
    # retire control chars (dont \n, \r, \t) -> espace
    s = "".join(" " if ord(c) < 32 else c for c in s)
    s = " ".join(s.split())          # normalise les espaces
    return s[:maxlen]


def _esc(s: str) -> str:
    """Échappe pour une chaîne AppleScript (double-quote). Entrée déjà
    sanitizée (pas de retour ligne) → pas d'évasion possible de la chaîne."""
    return _sanitize(s).replace("\\", "\\\\").replace('"', '\\"')


# --------------------------------------------------------------------------- #
# Construction du bundle de marque
# --------------------------------------------------------------------------- #
_APPLESCRIPT = (
    'on run\n'
    '\tset payloadPath to (POSIX path of (path to me)) & '
    '"Contents/Resources/payload.txt"\n'
    '\ttry\n'
    '\t\tset fh to open for access (POSIX file payloadPath)\n'
    '\t\tset txt to (read fh as «class utf8»)\n'
    '\t\tclose access fh\n'
    '\ton error\n'
    '\t\ttry\n'
    '\t\t\tclose access (POSIX file payloadPath)\n'
    '\t\tend try\n'
    '\t\treturn\n'
    '\tend try\n'
    "\tset AppleScript's text item delimiters to linefeed\n"
    '\tset L to text items of txt\n'
    '\tif (count of L) < 2 then return\n'
    '\tset theTitle to item 1 of L\n'
    '\tset theMsg to item 2 of L\n'
    '\tif (count of L) ≥ 3 and (item 3 of L) is not "" then\n'
    '\t\tdisplay notification theMsg with title theTitle subtitle '
    '(item 3 of L) sound name "default"\n'
    '\telse\n'
    '\t\tdisplay notification theMsg with title theTitle sound name "default"\n'
    '\tend if\n'
    'end run\n'
)


def _generate_icon_png(out_path: Path) -> bool:
    """Génère l'icône premium (V blanc sur carré charbon arrondi) en 1024px
    via rendu off-screen pyobjc. Aucune NSWindow → sûr hors main thread."""
    try:
        from AppKit import (NSBitmapImageRep, NSGraphicsContext, NSColor,
                            NSImage, NSBezierPath,
                            NSCompositingOperationSourceOver,
                            NSDeviceRGBColorSpace)
        from Foundation import NSMakeRect
    except Exception:
        return False
    glyph_path = ASSETS_DIR / "vocal-glyph-v-36.png"
    if not glyph_path.exists():
        return False
    try:
        SZ = 1024
        rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
            None, SZ, SZ, 8, 4, True, False, NSDeviceRGBColorSpace, 0, 0)
        ctx = NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.setCurrentContext_(ctx)
        inset = SZ * 0.08
        rect = NSMakeRect(inset, inset, SZ - 2 * inset, SZ - 2 * inset)
        radius = (SZ - 2 * inset) * 0.22
        NSColor.colorWithCalibratedRed_green_blue_alpha_(
            18 / 255, 20 / 255, 28 / 255, 1.0).set()
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            rect, radius, radius).fill()
        glyph = NSImage.alloc().initWithContentsOfFile_(str(glyph_path))
        if glyph is not None:
            gw = (SZ - 2 * inset) * 0.52
            gx = (SZ - gw) / 2
            glyph.drawInRect_fromRect_operation_fraction_(
                NSMakeRect(gx, gx, gw, gw),
                NSMakeRect(0, 0, glyph.size().width, glyph.size().height),
                NSCompositingOperationSourceOver, 1.0)
        NSGraphicsContext.restoreGraphicsState()
        png = rep.representationUsingType_properties_(4, None)  # 4 = PNG
        return bool(png.writeToFile_atomically_(str(out_path), True))
    except Exception:
        return False


def _build_icns(icon_png: Path, dest_icns: Path) -> bool:
    """PNG 1024 -> .icns via sips + iconutil."""
    import tempfile
    try:
        # Tempdir nettoyé en sortie de with (l'ancien mkdtemp fuyait à chaque
        # (re)construction du bundle).
        with tempfile.TemporaryDirectory(prefix="vlocal_iconset_") as tmp:
            iconset = Path(tmp) / "AppIcon.iconset"
            iconset.mkdir(parents=True, exist_ok=True)
            sizes = {
                "icon_16x16.png": 16, "icon_16x16@2x.png": 32,
                "icon_32x32.png": 32, "icon_32x32@2x.png": 64,
                "icon_128x128.png": 128, "icon_128x128@2x.png": 256,
                "icon_256x256.png": 256, "icon_256x256@2x.png": 512,
                "icon_512x512.png": 512, "icon_512x512@2x.png": 1024,
            }
            for name, sz in sizes.items():
                subprocess.run(["sips", "-z", str(sz), str(sz), str(icon_png),
                                "--out", str(iconset / name)],
                               check=True, capture_output=True, timeout=20)
            subprocess.run(["iconutil", "-c", "icns", str(iconset),
                            "-o", str(dest_icns)],
                           check=True, capture_output=True, timeout=20)
        return dest_icns.exists()
    except Exception:
        return False


def _build_bundle() -> bool:
    """Construit VlocalNotify.app (idempotent). True si prêt."""
    import shutil
    import tempfile
    try:
        APP_SUPPORT.mkdir(parents=True, exist_ok=True)
        # 1. Source AppleScript -> osacompile (tempdir nettoyé en sortie de with)
        with tempfile.TemporaryDirectory(prefix="vlocal_applet_") as tmp:
            src = Path(tmp) / "notify.applescript"
            src.write_text(_APPLESCRIPT, encoding="utf-8")
            if BUNDLE_PATH.exists():
                # ignore_errors : même sémantique d'échec silencieux que
                # l'ancien `rm -rf` en check=False (sans process externe).
                shutil.rmtree(BUNDLE_PATH, ignore_errors=True)
            subprocess.run(["osacompile", "-o", str(BUNDLE_PATH), str(src)],
                           check=True, capture_output=True, timeout=30)
        # 2. Icône
        icon_png = APP_SUPPORT / "_vlocal_icon.png"
        icns = BUNDLE_PATH / "Contents" / "Resources" / "AppIcon.icns"
        if _generate_icon_png(icon_png) and _build_icns(icon_png, icns):
            _plist_set("CFBundleIconFile", "AppIcon")
            # Le PNG source ne sert qu'à fabriquer l'.icns : on ne le laisse
            # pas à demeure dans App Support.
            icon_png.unlink(missing_ok=True)
        # 3. Info.plist : nom + identifiant + agent (pas d'icône dock)
        _plist_set("CFBundleName", "Vlocal")
        _plist_set("CFBundleIdentifier", "com.vlocal.notify")
        _plist_set("LSUIElement", "true", typ="bool")
        # 4. Signature ad-hoc (sinon Gatekeeper peut refuser après modif)
        subprocess.run(["codesign", "--force", "--deep", "-s", "-",
                        str(BUNDLE_PATH)], check=False, capture_output=True,
                       timeout=30)
        # Marqueur de version
        _BUNDLE_MARKER.write_text(_BUNDLE_VERSION, encoding="utf-8")
        return True
    except Exception as e:
        print(f"[notif]   construction bundle échouée ({e}) — repli osascript.")
        return False


def _plist_set(key: str, value: str, typ: str = "string"):
    plist = BUNDLE_PATH / "Contents" / "Info.plist"
    pb = "/usr/libexec/PlistBuddy"
    # Set si existe, sinon Add
    # v1.0.22 — timeout OBLIGATOIRE : ces deux appels étaient les seuls
    # subprocess.run SANS borne du chemin utilisateur. Ils sont atteints depuis
    # le SUPERVISEUR de dictée (notifier.show -> _ensure_bundle -> _build_bundle) :
    # un PlistBuddy figé tuait la boucle du superviseur -> plus aucune
    # auto-réparation pour le reste de la session.
    try:
        r = subprocess.run([pb, "-c", f"Set {key} {value}", str(plist)],
                           capture_output=True, timeout=10)
        if r.returncode != 0:
            subprocess.run([pb, "-c", f"Add {key} {typ} {value}", str(plist)],
                           capture_output=True, timeout=10)
    except subprocess.TimeoutExpired:
        print(f"[notif] PlistBuddy figé sur {key} -> abandonné (best-effort).")


def _ensure_bundle() -> bool:
    """Garantit le bundle de marque prêt (macOS uniquement). Mémoïsé."""
    global _bundle_ready
    if not _IS_MAC:
        return False
    if _bundle_ready is not None:
        return _bundle_ready
    with _bundle_lock:
        if _bundle_ready is not None:
            return _bundle_ready
        try:
            ok = False
            if (BUNDLE_PATH / "Contents" / "MacOS").exists() \
                    and _BUNDLE_MARKER.exists() \
                    and _BUNDLE_MARKER.read_text().strip() == _BUNDLE_VERSION:
                ok = True  # déjà construit, bonne version
            else:
                ok = _build_bundle()
            _bundle_ready = ok
        except Exception:
            _bundle_ready = False
        return _bundle_ready


def _notify_via_bundle(title: str, message: str, subtitle: Optional[str]) -> bool:
    """Écrit le payload et lance le bundle. True si lancé."""
    try:
        payload = BUNDLE_PATH / "Contents" / "Resources" / "payload.txt"
        # Sanitize : titre/message/subtitle sur 1 ligne chacun (le parsing du
        # bundle est ligne-à-ligne ; un \n dans le titre décalerait tout).
        payload.write_text(
            f"{_sanitize(title)}\n{_sanitize(message)}\n"
            f"{_sanitize(subtitle or '')}\n", encoding="utf-8")
        subprocess.run(["open", "-g", "-n", str(BUNDLE_PATH)],
                       check=True, capture_output=True, timeout=10)
        return True
    except Exception:
        return False


def _notify_via_osascript(title: str, message: str, subtitle: Optional[str],
                          sound: bool) -> bool:
    parts = [f'display notification "{_esc(message)}" with title "{_esc(title)}"']
    if subtitle:
        parts.append(f'subtitle "{_esc(subtitle)}"')
    if sound:
        parts.append('sound name "default"')
    try:
        r = subprocess.run(["osascript", "-e", " ".join(parts)],
                           check=False, timeout=5, capture_output=True,
                           text=True)
        if r.returncode != 0:
            # Échec rendu VISIBLE (ex. Automation refusée) : sans ce log,
            # aucune trace nulle part qu'une notification n'est jamais partie.
            print(f"[notif] osascript KO rc={r.returncode}: "
                  f"{(r.stderr or '').strip()}")
        return r.returncode == 0
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# v1.0.2 — UNUserNotificationCenter : la VOIE NATIVE MODERNE (macOS 11+).
#   - La notif s'affiche sous "Vlocal" avec l'ICÔNE DE L'APP, automatiquement
#     (aucun applet, aucun osascript -> plus JAMAIS "Éditeur de scripts").
#   - 1ʳᵉ fois : macOS demande l'autorisation pour "Vlocal" (prompt natif propre).
#   - Ne fonctionne QUE dans le .app signé (bundle id valide) ; en dev/python
#     currentNotificationCenter() échoue -> on retombe sur l'applet/osascript.
#   - Délégué de présentation : la bannière s'affiche même si Vlocal est au
#     premier plan (sinon macOS la masquerait pendant un test).
# --------------------------------------------------------------------------- #
_un_center = None
_un_delegate = None
_un_ready: Optional[bool] = None
_un_lock = threading.Lock()


def _present_opts() -> int:
    """Options de présentation au premier plan (Banner + List + Sound)."""
    try:
        from UserNotifications import (
            UNNotificationPresentationOptionBanner,
            UNNotificationPresentationOptionList,
            UNNotificationPresentationOptionSound)
        return (UNNotificationPresentationOptionBanner
                | UNNotificationPresentationOptionList
                | UNNotificationPresentationOptionSound)
    except Exception:
        return 16 | 8 | 2   # Banner(1<<4) | List(1<<3) | Sound(1<<1)


def _ensure_un() -> bool:
    """Prépare le centre de notifs natif (délégué + autorisation). Mémoïsé."""
    global _un_center, _un_delegate, _un_ready
    if not _IS_MAC:
        return False
    if _un_ready is not None:
        return _un_ready
    with _un_lock:
        if _un_ready is not None:
            return _un_ready
        try:
            # v1.0.6 — GARDE ANTI-CRASH (trouvée au smoke test) : sans bundle au
            # CFBundleIdentifier valide (lancement non packagé / atypique),
            # currentNotificationCenter() lève une NSException DANS un dispatch_once
            # -> NON rattrapable par try/except Python -> abort du process (exit 134).
            # On exige donc d'ABORD un bundle identifié ; sinon on renonce proprement
            # (repli applet/osascript). En prod (.app installé) l'id existe -> OK.
            from Foundation import NSBundle
            if not NSBundle.mainBundle().bundleIdentifier():
                _un_ready = False
                return False
            from UserNotifications import (
                UNUserNotificationCenter,
                UNAuthorizationOptionAlert, UNAuthorizationOptionSound)
            from Foundation import NSObject
            center = UNUserNotificationCenter.currentNotificationCenter()
            if center is None:
                _un_ready = False
                return False

            # Délégué : présentation au premier plan (bannière visible même si
            # Vlocal est l'app active). Défini une seule fois (mémoïsation).
            class _VlocalUNDelegate(NSObject):
                def userNotificationCenter_willPresentNotification_withCompletionHandler_(
                        self, c, n, handler):
                    try:
                        handler(_present_opts())
                    except Exception:
                        try:
                            handler(0)
                        except Exception:
                            pass

            _un_delegate = _VlocalUNDelegate.alloc().init()
            center.setDelegate_(_un_delegate)
            center.requestAuthorizationWithOptions_completionHandler_(
                UNAuthorizationOptionAlert | UNAuthorizationOptionSound,
                lambda granted, err: None)
            _un_center = center
            _un_ready = True
        except Exception as e:
            print(f"[notif] UN indisponible ({e}) — repli applet/osascript.")
            _un_ready = False
        return _un_ready


# v1.0.6 — STATUTS D'AUTORISATION (UNAuthorizationStatus) -> libellé stable.
_UN_STATUS = {0: "notDetermined", 1: "denied", 2: "authorized",
              3: "provisional", 4: "ephemeral"}
# Cache du dernier statut connu (évite un round-trip async à chaque rappel ; les
# rappels tournent sur un thread daemon, le test Réglages rafraîchit ce cache).
_un_auth_cache: Optional[str] = None


def auth_status(timeout: float = 2.0) -> str:
    """v1.0.6 — Statut RÉEL de l'autorisation de notification pour Vlocal.
    Renvoie : 'authorized' | 'denied' | 'notDetermined' | 'provisional' |
    'ephemeral' | 'unsupported' | 'unknown'. Best-effort, ne lève jamais.
    Pont async->sync (getNotificationSettings appelle son handler sur une file
    interne UN ; on attend sur un Event depuis un thread worker -> sûr)."""
    global _un_auth_cache
    if not _IS_MAC:
        return "unsupported"
    if not _ensure_un():
        return "unsupported"
    try:
        done = threading.Event()
        box = {"st": None}

        def _cb(settings):
            try:
                box["st"] = int(settings.authorizationStatus())
            except Exception:
                box["st"] = None
            done.set()

        _un_center.getNotificationSettingsWithCompletionHandler_(_cb)
        if not done.wait(timeout):
            return _un_auth_cache or "unknown"
        st = _UN_STATUS.get(box["st"], "unknown")
        _un_auth_cache = st
        return st
    except Exception:
        return _un_auth_cache or "unknown"


def request_authorization(timeout: float = 8.0) -> bool:
    """v1.0.6 — Déclenche le prompt d'autorisation natif et ATTEND la réponse.
    Renvoie True si accordée. Best-effort. (À appeler hors main thread.)"""
    global _un_auth_cache
    if not _ensure_un():
        return False
    try:
        from UserNotifications import (UNAuthorizationOptionAlert,
                                        UNAuthorizationOptionSound)
        done = threading.Event()
        box = {"granted": False}

        def _cb(granted, err):
            box["granted"] = bool(granted)
            done.set()

        _un_center.requestAuthorizationWithOptions_completionHandler_(
            UNAuthorizationOptionAlert | UNAuthorizationOptionSound, _cb)
        done.wait(timeout)
        _un_auth_cache = "authorized" if box["granted"] else _un_auth_cache
        return box["granted"]
    except Exception:
        return False


def _notify_via_un(title: str, message: str, subtitle: Optional[str],
                   sound: bool) -> bool:
    """Affiche une notif via UNUserNotificationCenter. True UNIQUEMENT si la notif
    a une chance RÉELLE d'apparaître (autorisation accordée). v1.0.6 — avant, on
    renvoyait True dès la remise au centre SANS vérifier l'autorisation : si elle
    était refusée/non accordée, UN jetait la notif en silence et `show()` ne
    repliait JAMAIS -> rappels muets. On vérifie donc le statut d'abord."""
    if not _ensure_un():
        return False
    # GARDE D'AUTORISATION : ne réclamer le succès que si la remise est plausible.
    st = auth_status()
    if st == "notDetermined":
        # 1ʳᵉ fois : on demande et on attend la réponse (le prompt natif Vlocal).
        if not request_authorization():
            return False           # refusé -> repli (bannière in-app garantie côté app)
    elif st in ("denied", "unsupported"):
        return False               # refusé/indispo -> repli, surtout pas de trou noir
    try:
        from UserNotifications import (
            UNMutableNotificationContent, UNNotificationRequest,
            UNNotificationSound)
        content = UNMutableNotificationContent.alloc().init()
        content.setTitle_(_sanitize(title))
        if subtitle:
            content.setSubtitle_(_sanitize(subtitle))
        content.setBody_(_sanitize(message))
        if sound:
            try:
                content.setSound_(UNNotificationSound.defaultSound())
            except Exception:
                pass
        ident = "vlocal-%d" % int(time.time() * 1000)
        req = UNNotificationRequest.requestWithIdentifier_content_trigger_(
            ident, content, None)   # trigger None = remise immédiate
        _un_center.addNotificationRequest_withCompletionHandler_(
            req, lambda err: None)
        return True
    except Exception as e:
        print(f"[notif] UN envoi KO ({e}) — repli applet/osascript.")
        return False


# --------------------------------------------------------------------------- #
# API publique
# --------------------------------------------------------------------------- #
def _notify_windows(title: str, message: str) -> bool:
    """Notification Windows (PowerShell BurntToast si présent, sinon ballon
    via WinForms). Best-effort, silencieux si indisponible."""
    try:
        ps = (
            "$ErrorActionPreference='SilentlyContinue';"
            "Add-Type -AssemblyName System.Windows.Forms;"
            "$n=New-Object System.Windows.Forms.NotifyIcon;"
            "$n.Icon=[System.Drawing.SystemIcons]::Information;"
            "$n.Visible=$true;"
            f"$n.ShowBalloonTip(6000,'{title}','{message}',"
            "[System.Windows.Forms.ToolTipIcon]::Info);"
            "Start-Sleep -Milliseconds 200"
        )
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       check=False, timeout=8)
        return True
    except Exception:
        return False


# v3 — Bundle de marque "Vlocal" PRIMAIRE : l'app est désormais packagée et
# signée (ad-hoc), la condition qui bloquait le branding. Les notifs s'affichent
# sous le nom "Vlocal" avec NOTRE icône (V sur charbon) au lieu de l'icône
# générique de "Script Editor" (osascript). Fiabilité préservée : si le bundle
# n'est pas prêt ou échoue à se lancer, on retombe sur osascript ; et pour les
# rappels, app.py double TOUJOURS la notif d'une bannière in-app. Mettre
# VLOCAL_BRANDED_NOTIF=0 pour forcer l'ancien comportement osascript.
_USE_BRANDED = os.environ.get("VLOCAL_BRANDED_NOTIF", "1") in ("1", "true", "True")


_USE_UN = os.environ.get("VLOCAL_UN_NOTIF", "1") in ("1", "true", "True")


def show(title: str, message: str, subtitle: Optional[str] = None,
         sound: bool = True) -> bool:
    """Notification immédiate, cross-platform.
    v1.0.7 — ORDRE CORRIGÉ (bug terrain « rappels muets / Éditeur de scripts ») :
    1) applet de marque Vlocal (icône Vlocal, FIABLE : open+AppleScript, prouvé) ;
    2) UNUserNotificationCenter (repli) ; 3) osascript (dernier repli).
    POURQUOI : dans le bundle PyInstaller notarisé, addNotificationRequest (UN) est
    ACCEPTÉ mais la bannière n'est JAMAIS rendue par usernoted -> `_notify_via_un`
    renvoyait True (faux succès) et on ne repliait jamais -> ZÉRO notif de rappel.
    L'applet de marque, lui, passe à coup sûr et avec notre icône. On le met devant."""
    if DRY_RUN:
        return True
    if not _IS_MAC:
        return _notify_windows(title, message)
    # v1.0.7c — UN (com.vlocal.app) en PRIMAIRE, MAINTENANT qu'il est autorisé :
    # sur macOS récent `display notification` passe par UN, et osascript s'affiche
    # (preuve que UN marche pour une app autorisée). com.vlocal.app a reçu
    # l'autorisation -> UN doit livrer une notif BRANDÉE Vlocal. osascript reste
    # le repli garanti si UN ne livre pas.
    if _USE_UN and _notify_via_un(title, message, subtitle, sound):
        return True
    if _notify_via_osascript(title, message, subtitle, sound):
        return True
    if _USE_BRANDED and _ensure_bundle() and _notify_via_bundle(title, message, subtitle):
        return True
    return False


def parse_iso(s: Optional[str]) -> Optional[float]:
    """Parse une chaîne ISO 8601 / "YYYY-MM-DD HH:MM" -> timestamp Unix.
    Un suffixe « Z » est traité comme UTC (datetime aware, géré nativement par
    fromisoformat en 3.11+) ; sinon le naïf est interprété en heure locale.
    None si invalide."""
    if not s or not isinstance(s, str):
        return None
    import datetime as _dt
    try:
        clean = s.strip()
        if clean.endswith("Z"):
            # UTC explicite : surtout NE PAS retirer le Z puis parser naïf
            # (le timestamp serait décalé de l'offset du fuseau local).
            return _dt.datetime.fromisoformat(clean).timestamp()
        if "T" not in clean and " " not in clean and len(clean) == 10:
            clean += "T00:00:00"
        return _dt.datetime.fromisoformat(clean).timestamp()
    except Exception:
        return None


def prebuild_async():
    """v15 — Construit le bundle en arrière-plan au démarrage pour que la
    1ʳᵉ vraie notif soit instantanée (et la permission demandée tôt)."""
    def _w():
        try:
            # v1.0.2 — demande l'autorisation native TÔT (prompt "Vlocal" propre)
            # pour que la 1ʳᵉ vraie notif soit instantanée et déjà autorisée.
            _ensure_un()
        except Exception:
            pass
        try:
            _ensure_bundle()   # repli prêt aussi
        except Exception:
            pass
    threading.Thread(target=_w, daemon=True).start()
