"""Dictée : capture, finalisation, livraison du texte.

DictationController est partagé par le bouton Dicter, la barre Espace, le menu
V et le raccourci global. start() ouvre le micro (worker jetable, escalade
bornée en cas d'échec) ; stop_and_finalize() ferme le micro tout de suite puis
transcrit dans une file FIFO, sous le budget de _finalize_budget_s, avant de
copier et d'insérer le texte. Le superviseur répare une dictée coincée sans
que l'utilisateur ait à relancer l'app. Détail du pipeline :
docs/ARCHITECTURE.md, « Dictation pipeline ».
"""
import re
import threading
import time

import applog
import appstate
import errors as _EV
import notifier
import overlay
import permissions
import processor
from engine import VlocalEngine
from lifecycle import _begin_activity, _end_activity, _self_relaunch
from reminder_flow import _looks_like_timed_reminder, _post_process_by_mode
from settings import _current_mode, _load_settings, _save_settings
from texts import (
    _EMPTY_TEXT_MSG,
    _LATE_PENDING_OVERLAY,
    _LATE_PENDING_TOAST,
    _LATE_SAVED_OVERLAY,
    _LATE_SAVED_TOAST,
    _LOST_DICTATION_OVERLAY,
    _LOST_DICTATION_TOAST,
    _te,
)
from ui_bridge import _msg, _refresh_lists_ui, _schedule_overlay_hide, _toast, _ui

log = applog.get("dictation")


def copy_to_clipboard(text: str) -> bool:
    """Copie le texte dans le presse-papier (macOS).
    La copie auto EST le produit (coller avec Cmd+V)."""
    if not text:
        return False
    try:
        import clipboard
        return clipboard.set_clipboard(text)
    except Exception:
        log.debug("import clipboard : échec ignoré", exc_info=True)
        return False


# --------------------------------------------------------------------------- #
# v1.0.18 — Garde-fous micro / insertion : messages CLAIRS (jamais de spam),
# 100 % best-effort, in-process (aucun subprocess), JAMAIS d'exception propagée.
# Holders mutables (pas de `global` dans de grosses méthodes) :
# --------------------------------------------------------------------------- #
_acc_warned = [False]     # Accessibilité OFF : signalée UNE SEULE FOIS / session
_mic_fail_count = [0]     # échecs micro consécutifs -> détection coreaudiod figé
# v1.0.22 — chaînage : sérialise les INSERTIONS au curseur (deux dictées
# enchaînées ne doivent jamais entrelacer leurs collages ; l'ordre FIFO est déjà
# garanti par les tickets de finalisation, ce verrou empêche le chevauchement).
_insert_serial_lock = threading.Lock()


def _time_left(deadline: float) -> float:
    """v1.0.22 — secondes restantes avant `deadline` (horloge monotone).
    Socle du budget « jamais bloqué > 2 s » : chaque étape de l'escalade micro
    n'est tentée que si le budget de l'appui le permet encore."""
    return deadline - time.monotonic()


# Enregistreurs / visio connus qui tiennent souvent le micro -> message ciblé.
_MIC_GRABBERS = (
    ("screen studio", "Screen Studio"), ("obs", "OBS"), ("zoom", "Zoom"),
    ("microsoft teams", "Teams"), ("quicktime", "QuickTime Player"),
    ("loom", "Loom"), ("cleanshot", "CleanShot"), ("screenflow", "ScreenFlow"),
    ("camtasia", "Camtasia"), ("webex", "Webex"), ("discord", "Discord"),
    ("facetime", "FaceTime"),
)


def _mic_grabber_name():
    """Nom d'un enregistreur/visio connu actuellement lancé (ou None). Best-effort,
    in-process (NSWorkspace.runningApplications, pas de `ps`), jamais d'exception.
    Appelé seulement sur le chemin d'ÉCHEC micro (rare) -> coût négligeable."""
    try:
        from AppKit import NSWorkspace
        for app in (NSWorkspace.sharedWorkspace().runningApplications() or []):
            try:
                low = (app.localizedName() or "").lower()
            except Exception:
                log.debug("localizedName() or '').lower : échec ignoré", exc_info=True)
                continue
            for needle, label in _MIC_GRABBERS:
                if needle in low:
                    return label
    except Exception:
        log.debug("import AppKit : échec ignoré", exc_info=True)
    return None


# v1.1.0 : le live-tail vit dans son module (livetail.py), testable sans
# charger l'app. v1.7 : c'est le découpeur unique (pauses et fenêtres longues).
from livetail import DictationLiveTail as _DictationLiveTail  # noqa: E402


# Plancher du budget de finalisation d'une dictée (live-tail + fusion + insertion
# compris). Le budget réel est _finalize_budget_s(durée) : proportionnel à l'audio
# depuis la v1.1.0, jamais sous ce plancher. Quand il est dépassé, le chien de
# garde libère le verrou et dit à l'utilisateur que la dictée est perdue.
FINALIZE_WATCHDOG_S = 25.0
# v1.4.4 — marge au-delà du budget de TRANSCRIPTION (GPU puis repli CPU, fixé
# par engine.finalize_budget_s, qui ne couvre que le moteur) : fusion, filtres,
# glossaire, copie presse-papier. Ces étapes prennent des millisecondes ; 3 s
# absorbent un presse-papier ou un disque lent sans retarder le verdict d'une
# vraie panne.
FINALIZE_MARGIN_S = 3.0


# --------------------------------------------------------------------------- #
# v1.0.9 — SUPERVISEUR DE DICTÉE (auto-heal). Garantit qu'un utilisateur n'est
# JAMAIS bloqué plus de quelques secondes. La cause racine (figeage audio au
# stop/open) est déjà neutralisée par les fermetures/ouvertures BORNÉES du moteur
# (récupération ~3 s). Ce superviseur est le filet GLOBAL pour TOUT le reste :
#   1) si la finalisation reste coincée > seuil (au-dessus du timeout MLX 22 s
#      pour ne jamais couper une vraie transcription) -> reset propre + notif ;
#   2) si ça se recoince malgré plusieurs resets -> dernier recours : l'app se
#      relance toute seule (garde-fou anti-boucle, l'utilisateur n'a rien à faire).
# --------------------------------------------------------------------------- #
import engine as _engine_mod  # noqa: E402


def _finalize_budget_s(rec_dur_s: float) -> float:
    """Plafond de la finalisation d'une dictée de rec_dur_s secondes.
    v1.1.0 : proportionnel à l'audio (le plafond fixe de 25 s coupait les dictées
    de plusieurs minutes et les déclarait figées). v1.4.4 : suit le budget de
    transcription du moteur (engine.finalize_budget_s : GPU PUIS repli CPU) plus
    FINALIZE_MARGIN_S. L'ancien « timeout GPU + 3 s » ne laissait rien au repli
    CPU : une dictée sauvée par le processeur était coupée par ce chien de
    garde. Plancher FINALIZE_WATCHDOG_S."""
    return max(FINALIZE_WATCHDOG_S,
               _engine_mod.finalize_budget_s(rec_dur_s) + FINALIZE_MARGIN_S)


_DICT_RESET_MIN_S = 9.0       # v1.0.11 : 12 -> 9 s. PLANCHER SÛR : la formule garde +8 de base (marge au-dessus du pire cas de transcription LÉGITIME ~6-8 s : modèles rechargés + plancher turbo + ladder température) -> on ne descend PAS sous 9 sinon risque de couper une vraie dictée. Les dictées longues montent via durée×0.10+8.


def _dictation_supervisor():
    """Daemon : surveille l'état de la dictée et garantit la récupération."""
    import time as _t
    resets = []          # horodatages des resets récents (détection re-coincement)
    last_reset = 0.0
    last_health = 0.0    # v1.0.22 — dernier check de santé du worker micro
    while True:
        _t.sleep(1.0)
        try:
            c = appstate.controller
            if c is None:
                continue
            busy = bool(getattr(c, "_busy", False))
            active = bool(getattr(c, "_active", False))
            # v1.0.22 — SANTÉ PROACTIVE du worker micro : toutes les ~10 s AU
            # REPOS, ping du worker ; figé/mort -> respawn préventif EN FOND.
            # L'utilisateur ne rencontre (presque) jamais un worker coincé à
            # l'appui : il a été remplacé pendant l'inactivité.
            if (not busy) and (not active) and (_t.time() - last_health) > 10.0:
                last_health = _t.time()
                try:
                    _eng = getattr(c, "engine", None)
                    if _eng is not None and hasattr(_eng, "mic_health_heal"):
                        threading.Thread(target=_eng.mic_health_heal,
                                         name="mic-health", daemon=True).start()
                except Exception:
                    log.debug("getattr : échec ignoré", exc_info=True)
            ft0 = float(getattr(c, "_finalize_t0", 0.0) or 0.0)
            t0 = float(getattr(c, "_t0", ft0) or ft0)
            now = _t.time()
            # COINCÉ = occupé, plus en enregistrement (utilisateur a relâché), et la
            # finalisation ne rend pas la main depuis trop longtemps. On mesure depuis
            # le DÉBUT de la finalisation (ft0). PLAFOND ADAPTATIF : 12 s pour une
            # dictée courte/moyenne (récup < 15 s visée), un peu plus pour une dictée
            # très longue dont la finalisation légitime dure plus. La marge
            # (0,10 x durée + 8) reste TOUJOURS au-dessus du temps de transcription
            # réel (~0,045 x durée) et du timeout moteur 22 s -> on ne coupe JAMAIS
            # une vraie transcription.
            rec_dur = max(0.0, ft0 - t0)
            ceiling = max(_DICT_RESET_MIN_S, rec_dur * 0.10 + 8.0)
            # v1.0.22 — on mesure l'INACTIVITÉ (dernier battement), pas la durée
            # totale : une transcription lente mais qui progresse n'est plus
            # déclarée coincée (sinon l'epoch était bumpé et le texte, arrivé
            # après, n'était jamais inséré). Repli sur ft0 si aucun battement.
            beat = float(getattr(c, "_fin_beat", 0.0) or 0.0) or ft0
            is_stuck = busy and (not active) and ft0 > 0 and (now - beat) > ceiling
            # v1.0.11 — FILET SUPPLÉMENTAIRE : un verrou « occupé » coincé SANS qu'une
            # finalisation ait démarré (ft0 == 0) n'était couvert par aucun garde-fou
            # -> « transcription précédente en cours » à vie. On le récupère aussi
            # (seuil prudent 6 s, bien au-dessus d'une finalisation qui démarre
            # normalement dans la seconde -> aucun faux positif sur une vraie dictée).
            bs = float(getattr(c, "_busy_since", 0.0) or 0.0)
            stale_busy = busy and (not active) and ft0 <= 0 and bs > 0 and (now - bs) > 5.0
            # v1.0.11 — DICTÉE ACTIVE FIGÉE : enregistrement « actif » mais le worker
            # audio est MORT (crash inattendu) -> plus de wave, plus de détection, et
            # stop_and_finalize ne viendra jamais. Cas genuinement cassé (en dictée
            # normale le worker tourne jusqu'au stop). Garde 2 s pour ignorer la
            # fenêtre de démarrage (active posé juste avant le lancement du worker).
            t0_rec = float(getattr(c, "_t0", 0.0) or 0.0)
            wkr = getattr(c, "_audio_worker", None)
            active_dead = active and (wkr is None or not wkr.is_alive()) and t0_rec > 0 and (now - t0_rec) > 2.0
            if not (is_stuck or stale_busy or active_dead):
                continue
            if now - last_reset < 8.0:   # ne pas spammer
                continue
            last_reset = now
            resets[:] = [t for t in resets if now - t < 60.0] + [now]
            log.warning(f"[auto-heal] dictée coincée ({now - ft0:.0f}s) -> reset auto (#{len(resets)}).")
            try:
                c.force_reset("superviseur")
            except Exception:
                log.debug("c.force_reset : échec ignoré", exc_info=True)
            try:
                overlay.info(_te("Dictée réparée"),
                             _te("La dernière dictée n'a pas abouti. Vous pouvez recommencer."))
                _schedule_overlay_hide(4.0)
            except Exception:
                log.debug("overlay.info : échec ignoré", exc_info=True)
            try:
                # v1.0.22 — EN THREAD : notifier.show peut construire le bundle
                # de marque (sips/PlistBuddy, plusieurs secondes). Appelé en
                # synchrone, il retardait — voire tuait — la boucle du
                # superviseur, c'est-à-dire l'auto-réparation elle-même.
                threading.Thread(
                    target=lambda: notifier.show(
                        _te("Dictée réparée"),
                        _te("La dernière dictée n'a pas abouti. Vous pouvez "
                            "recommencer, Vlocal est de nouveau prêt."),
                        subtitle=None),
                    name="heal-notify", daemon=True).start()
            except Exception:
                log.debug("start : échec ignoré", exc_info=True)
            # Re-coincement répété MALGRÉ les resets -> sous-système audio OS
            # irrécupérable -> dernier recours : relance auto (garde-fou interne).
            if len(resets) >= 3:
                _self_relaunch("dictée coincée à répétition")
        except Exception:
            log.debug("bool : échec ignoré", exc_info=True)
            continue


def _start_dictation_supervisor():
    try:
        threading.Thread(target=_dictation_supervisor, name="dict-supervisor",
                         daemon=True).start()
        log.info("[auto-heal] superviseur de dictée actif.")
    except Exception as e:
        log.warning(f"[auto-heal] superviseur indispo ({e}).")


def _polish_dictation(raw: str) -> str:
    """Texte brut du moteur -> texte de la dictée (filtre anti-hallucination,
    glossaire, raccourcis vocaux, commandes de mise en forme). Sans effet de
    bord : partagé par la livraison normale et la dictée tardive. "" si rien ne
    reste."""
    # v3.2.8 — FILTRE anti-hallucination POST-moteur (en plus de celui d'engine.py,
    # qu'on NE touche PAS). Attrape ce qui glisse : crédits de sous-titres hallucinés
    # ("Transcription by CastingWords", "Sous-titres réalisés par…", amara.org) et
    # boucles de répétition ("Er Er Er…" : un mot court répété 6x+ -> 1). Ne déclenche
    # JAMAIS sur une vraie phrase (seuil 6 répétitions consécutives identiques).
    try:
        raw = re.sub(r"(?i)(transcription by castingwords|sous-titr\w*\s+(réalisé|fait|par|pour)\b[^.\n]*|sous-titrage\s+société radio-canada|amara\.org|♪+)", "", raw)
        raw = re.sub(r"(?i)\b(\w{1,4})(?:[\s,]+\1\b){5,}", r"\1", raw)
        raw = re.sub(r"\s{2,}", " ", raw).strip()
    except Exception as e:
        log.warning(f"[dictée] filtre anti-hallucination ignoré : {e}")
    if not raw:
        return ""
    # Vlocal 2 — Correction GLOSSAIRE sur la dictée (noms propres/acronymes),
    # quel que soit le modèle utilisé par la cascade. Fidélité garantie même
    # quand small a fast-pathé.
    try:
        if appstate.store is not None:
            gl = appstate.store.glossary_pairs()
            if gl:
                raw = processor.apply_glossary(raw, gl)
    except Exception as e:
        log.warning(f"[dictée] glossaire ignoré : {e}")
    # Vlocal 2 — Raccourcis vocaux (snippets) : on dicte « ma signature »
    # -> insertion du bloc enregistré. Appliqué sur TOUS les modes.
    try:
        if appstate.store is not None:
            pairs = appstate.store.snippet_pairs()
            if pairs:
                raw = processor.apply_glossary(raw, pairs)
    except Exception as e:
        log.warning(f"[dictée] raccourcis vocaux ignorés : {e}")
    # v1.0.14 — Commandes vocales de mise en forme (« à la ligne », « virgule »…),
    # APRÈS le glossaire/snippets et AVANT la copie/insertion. DICTÉE uniquement
    # (jamais en RÉUNION, qui a son propre pipeline). La moindre erreur -> texte
    # inchangé.
    try:
        if _current_mode() != "REUNION":
            raw = processor.apply_vocal_commands(raw)
    except Exception as e:
        log.warning(f"[dictée] commandes vocales ignorées : {e}")
    return raw


def _history_keeps_dictations() -> bool:
    """Le réglage de stockage garde-t-il le texte des dictées (« all ») ?"""
    return appstate.store is not None and _load_settings().get("history_scope", "all") == "all"


def _late_dictation_messages() -> tuple:
    """(toast, overlay) quand le chien de garde déclare une dictée en retard :
    elle ne sera pas insérée ; si l'historique la garde, on dit où la retrouver
    si elle finit par arriver, sinon on dit qu'elle est perdue."""
    try:
        keeps = _history_keeps_dictations()
    except Exception as e:
        log.warning(f"[dictée] réglage d'historique illisible : {e}")
        keeps = False
    if keeps:
        return _LATE_PENDING_TOAST, _LATE_PENDING_OVERLAY
    return _LOST_DICTATION_TOAST, _LOST_DICTATION_OVERLAY


class _DictationStop(threading.Event):
    """Événement d'arrêt PROPRE À UNE dictée (v1.0.22), qui porte aussi ce que le
    worker de niveau a constaté pendant l'enregistrement (v1.4.4) : un flux mort
    déjà journalisé n'est pas compté une seconde fois à la finalisation."""

    def __init__(self):
        super().__init__()
        self.stream_dead = False


class _FinalizeWatch:
    """Chien de garde et battement de cœur d'UNE finalisation de dictée.

    v1.4.4 :
      - le chien de garde est ARMÉ quand vient le tour de la dictée dans la file
        FIFO (arm), plus au relâchement : une dictée enchaînée derrière une
        dictée lente n'est plus déclarée en retard pour le temps passé à attendre ;
      - claim() tranche, sous le verrou de la file, entre « résultat livré » et
        « déclaré en retard » : jamais les deux, donc un seul incident et un seul
        message par appui ;
      - le battement (superviseur) couvre l'attente puis le budget, et se tait
        au-delà : une vraie panne redevient visible.
    `on_fire` est appelé une fois, hors verrou, si le budget est dépassé."""

    def __init__(self, cv, budget_s, on_fire, on_beat):
        self._cv = cv
        self.budget_s = budget_s
        self._on_fire = on_fire
        self._on_beat = on_beat
        self._done = threading.Event()
        self._settled = False   # claim(), le déclenchement ou disarm() a tranché
        self.fired = False
        self.beat_until = time.time() + budget_s

    def start(self):
        """Au relâchement : battement de cœur seul."""
        threading.Thread(target=self._beat_loop, name="finalize-beat", daemon=True).start()

    def arm(self):
        """Au tour de la dictée dans la file : le budget commence maintenant."""
        self.beat_until = time.time() + self.budget_s
        threading.Thread(target=self._watch, name="finalize-watchdog", daemon=True).start()

    def claim(self) -> bool:
        """True : le résultat sera livré, le chien de garde ne déclenchera plus.
        False : la dictée a déjà été déclarée en retard, rien ne doit être livré."""
        with self._cv:
            self._settled = True
            return not self.fired

    def disarm(self):
        """Fin de la finalisation, quelle qu'en soit l'issue."""
        with self._cv:
            self._settled = True
        self._done.set()

    def _watch(self):
        if self._done.wait(self.budget_s):
            return
        with self._cv:
            if self._settled:
                return
            self._settled = self.fired = True
        self._on_fire()

    def _beat_loop(self):
        # Ne s'arrête qu'à la fin de la finalisation : arm() peut repousser
        # beat_until après une longue attente dans la file. Au-delà, il se tait
        # et laisse voir la panne.
        while not self._done.wait(2.0):
            if time.time() < self.beat_until:
                self._on_beat()


# Périphériques qui captent le son du SYSTÈME (visio en réunion) : utiles à la
# réunion, jamais à la dictée (on y dicterait le son de l'ordinateur).
_LOOPBACK_KW = ("blackhole", "aggregate", "multi-output", "loopback",
                "stereo mix", "soundflower", "vb-audio", "voicemeeter",
                "what u hear", "wave out", "microsoft teams", "zoomaudio",
                "zoom audio")


class DictationController:
    # v1.5 — Le chargement du modèle (prewarm) et l'ouverture du micro se
    # disputaient le CPU et le lecteur du worker dans les premières centaines
    # de millisecondes (bascule HFP, premiers blocs CoreAudio) : trous et faux
    # flux mort. On laisse la capture s'installer d'abord ; le chargement reste
    # masqué par la parole, qui dure toujours bien plus. (Le découpeur ne
    # transcrit rien avant 2 s de parole : il n'a pas besoin de ce délai.)
    CAPTURE_SETTLE_S = 0.3

    def __init__(self, engine: VlocalEngine):
        self.engine = engine
        # v1.5 — ouverture lente (Bluetooth) : l'overlay dit « connexion du micro ».
        self._connecting_shown = False
        engine.on_mic_connecting = self._mic_connecting
        self._stop_evt = _DictationStop()
        # v1.4.4 — texte court de l'overlay quand la finalisation rend "" (silence,
        # panne, dictée perdue...). Par THREAD : le raccourci lit, dans le thread
        # qui a appelé stop_and_finalize, le verdict de SA dictée, pas celui
        # d'une dictée enchaînée.
        self._outcome = threading.local()
        self._audio_worker = None       # v13.1 — référence du worker audio_level
        self._active = False
        # v19 — VERROU ANTI-CHEVAUCHEMENT : True de start() jusqu'à la FIN de
        # stop_and_finalize (transcription comprise). Empêche une 2e dictée
        # (bouton OU raccourci) de démarrer pendant qu'une transcription tourne
        # encore -> le moteur n'est pas réentrant, le chevauchement le bloquait
        # (« ça tourne dans le vide »). Auto-réparation si bloqué trop longtemps.
        self._busy = False
        self._busy_since = 0.0
        self._gen = 0   # CONC-5 : n° de dictée (un _finalize tardif ne doit pas
                        # écraser l'overlay d'une dictée plus récente)
        self._lock = threading.Lock()
        self._activity = None   # v21 — assertion anti-App Nap (begin/end)
        self._tail = None       # v21 — live-tail des dictées longues
        self._finalize_t0 = 0.0  # v1.0.9 — horloge de finalisation (superviseur auto-heal)
        self._mic_lost = False   # v1.0.11 — flux audio mort détecté -> ré-init profonde au prochain start
        # v1.0.22 — DICTÉE ENCHAÎNÉE : la capture (worker micro, process séparé)
        # est indépendante de la transcription -> on peut ENREGISTRER la dictée
        # suivante PENDANT que la précédente se finalise (usage intensif : fini
        # le refus « transcription précédente en cours » = fini l'audio perdu).
        # Ordre garanti par tickets FIFO ; validité d'insertion par _epoch
        # (bumpé UNIQUEMENT par force_reset — un start enchaîné n'invalide PLUS
        # la finalisation précédente, contrairement à _gen qui garde son rôle
        # de fraîcheur d'overlay).
        self._fin_beat = 0.0     # v1.0.22 — battement de cœur de la finalisation
        self._fin_cv = threading.Condition()
        self._fin_inflight = 0   # finalisations en vol (plafond 3)
        self._fin_next = 0       # prochain ticket FIFO à attribuer
        self._fin_serving = 0    # ticket FIFO en cours de service
        self._epoch = 0          # invalidation d'insertion (force_reset seul)

    def recording_active(self) -> bool:
        # v1.0.22 — LECTURE SANS VERROU (atomique sous le GIL). Avant, ce
        # getter prenait self._lock, que start() tient pendant TOUTE l'escalade
        # micro -> le end() du raccourci (relâchement) se bloquait derrière un
        # begin() en difficulté, y compris le consommateur de secours du pump
        # watchdog : le mécanisme censé secourir la dictée était lui-même
        # bloqué. C'était la cause du « je dois relancer l'appli ».
        return self._active

    def recording_duration(self) -> float:
        """Durée écoulée depuis le début de la dictée la plus récente (s).
        ≈0 si aucune dictée n'a encore démarré."""
        return max(0.0, time.time() - getattr(self, "_t0", time.time()))

    def start(self) -> bool:
        with self._lock:
            if self._active:
                return False
            # v1.0.22 — DICTÉE ENCHAÎNÉE : on ne refuse PLUS quand une
            # finalisation est en cours (l'ancien « démarrage refusé :
            # finalisation en cours » faisait PERDRE la dictée suivante en
            # usage intensif — l'utilisateur parlait dans le vide). La capture
            # (worker micro) est indépendante de la transcription ; l'ordre des
            # insertions est garanti par les tickets FIFO de stop_and_finalize.
            # Seul garde-fou : plafond de 3 finalisations en vol (sanité RAM ;
            # humainement inatteignable sauf pathologie -> le superviseur agit).
            # Le rattrapage « verrou périmé » de la v1.0.9 disparaît avec le
            # verrou lui-même (le filet stale_busy du superviseur reste).
            if self._fin_inflight >= 3:
                log.warning("[dictée] démarrage refusé : 3 finalisations déjà en vol.")
                return False
            # v1.0.11 — si le flux micro est mort à la dictée précédente (détecteur de
            # flux mort), on ré-initialise PortAudio EN PROFONDEUR AVANT de rouvrir ->
            # 1re tentative propre, l'utilisateur ne subit pas un échec + retry.
            # v1.0.22 — BUDGET DUR DE L'APPUI : quoi qu'il arrive, start() rend
            # la main sous ~2 s. Au-delà, on ne fait pas patienter l'utilisateur
            # (il parlait dans le vide, parfois 100 s : 22 appuis en 107 s
            # observés au pire incident) — on rend la main, on l'informe, et on
            # RÉPARE EN FOND pour l'appui suivant (terrain : il réappuie sous 3 s).
            _budget_dl = time.monotonic() + 2.0
            if getattr(self, "_mic_lost", False):
                # v1.5 — flux mort à la dictée précédente et réparation de fond
                # pas encore lancée : worker neuf LANCÉ sans l'attendre ;
                # l'ouverture qui suit le rejoint. Avant, ce reset synchrone
                # prélevait 1 s fixe sur les 2 s du budget avant tout essai.
                self._mic_lost = False
                self.engine.hard_mic_reset(wait=False)
            # v13.1 — Bug observé : si on relançait une dictée alors qu'un
            # ancien worker audio était encore en train de boucler (cas rare
            # mais possible si stop_and_finalize a été interrompu), on
            # accumulait des threads zombies qui spammaient setAudioLevel
            # → latence UI cumulative. On nettoie l'éventuel ancien worker.
            if self._audio_worker is not None and self._audio_worker.is_alive():
                self._stop_evt.set()
                # v1.0.22 — 2,0 -> 0,3 s : ce join est sur le chemin de l'APPUI.
                # Le worker ne fait que pousser un niveau à 10 Hz ; s'il traîne,
                # son event est déjà posé et il meurt de lui-même (daemon).
                self._audio_worker.join(timeout=0.3)
            self._audio_worker = None
            # v1.0.22 — événement d'arrêt PAR DICTÉE (objet NEUF, pas un clear) :
            # avec le chaînage, la finalisation de la dictée précédente a posé
            # set() sur SON event ; un clear() partagé pouvait le lui retirer
            # avant que son worker de niveau ne l'ait vu (worker zombie).
            self._stop_evt = _DictationStop()
            # v1.5 — assertion anti-App Nap AVANT l'ouverture du micro : c'est
            # l'ouverture (et ses premiers blocs) que le throttling retardait.
            self._activity = _begin_activity("Vlocal dictation")
            self._connecting_shown = False
            _device = self._dictation_device()
            # P1-2 — micro indisponible / permission révoquée : start_recording LÈVE
            # (engine.py). v1.0.11 — AUTO-HEAL MICRO : un device CoreAudio coincé fait
            # échouer l'ouverture. Au lieu d'abandonner (et de forcer un redémarrage
            # de l'app), on ré-initialise PortAudio EN PROFONDEUR puis on RÉESSAIE une
            # fois -> récupération sur place, l'utilisateur n'a rien à faire. Le « micro
            # indisponible » est désormais AUSSI journalisé (avant : print/toast seuls
            # -> invisible dans le diagnostic). (_active/_busy pas encore posés : rien
            # à nettoyer en cas d'échec.)
            ok = False
            try:
                ok = self.engine.start_recording(device=_device, deadline=_budget_dl)
                # v1.0.22 — chaînage : juste après un relâchement, la capture
                # précédente peut être en train de se refermer (fenêtre ~0,3 s :
                # jointure du worker de niveau + stop du flux). False ici =
                # « moteur encore en enregistrement » -> on re-tente jusqu'à
                # épuisement du budget (résolu en ~0,1 s en pratique).
                while (not ok) and _time_left(_budget_dl) > 0.05:
                    time.sleep(0.05)
                    ok = self.engine.start_recording(device=_device, deadline=_budget_dl)
                if not ok:
                    log.warning("[dictée] capture précédente toujours ouverte (budget épuisé).")
            except Exception as e:
                log.warning(f"[dictée] micro indisponible : {e} -> reset dur + retry.")
                # v1.4.4 — UN incident par appui : chaque étape en échec est
                # retenue, seule l'issue finale est journalisée (errors.MicStartTrace).
                _trace = _EV.MicStartTrace() if _EV else None
                _via, _dev_ok = None, None
                if _trace: _trace.fail("start", e)
                # v1.0.22 — ESCALADE BÉTON. Terrain 1.0.21 (diagnostics 24/07-10/08) :
                # 28 échecs « start », CHAQUE retry en échec aussi, 0 mic_recovered —
                # la ré-init in-process ne récupère JAMAIS un PortAudio corrompu dans
                # ce process (threads de close abandonnés coincés dedans). Le geste qui
                # marche toujours, c'est un PROCESS NEUF (relancer l'app). Désormais :
                #   1) hard_mic_reset = kill -9 + respawn du WORKER micro (état
                #      CoreAudio neuf, borné ~1 s) — plus une ré-init à l'aveugle ;
                #   2) retry après un délai (CoreAudio relâche le device) ;
                #   3) repli micro INTÉGRÉ résolu CÔTÉ WORKER (l'énumération
                #      in-process d'un parent coincé mentait) ;
                #   4) CHAQUE étape tracée — le trou qui a rendu la 1.0.21
                #      indiagnosticable (échec du repli 100% silencieux) est bouché.
                #      v1.4.4 : la chaîne complète part dans UN seul événement.
                # v1.0.22 — chaque étape est conditionnée au BUDGET RESTANT :
                # mieux vaut rendre la main en 2 s et réparer en fond que faire
                # patienter l'utilisateur (jusqu'à 134 s au pire cas théorique).
                # v1.5 — le respawn est réservé à un worker indisponible : un
                # refus du périphérique (engine.MicRefused, micro intégré déjà
                # tenté) laisse le worker sain, le tuer coûtait 1,3 s.
                if (not getattr(e, "worker_healthy", False)
                        and _time_left(_budget_dl) > 0.5):
                    try:
                        self.engine.hard_mic_reset(timeout=min(1.2, _time_left(_budget_dl)))
                    except Exception as e_reset:
                        log.warning(f"[dictée] reset dur du micro en échec : {e_reset}")
                    time.sleep(min(0.2, max(0.0, _time_left(_budget_dl))))  # CoreAudio relâche
                try:
                    if _time_left(_budget_dl) <= 0.05:
                        raise RuntimeError("budget épuisé")
                    ok = self.engine.start_recording(device=_device, deadline=_budget_dl)
                    if ok:
                        log.info("[dictée] micro récupéré après reset dur (worker neuf).")
                        _via = "hard_reset"
                except Exception as e2:
                    log.warning(f"[dictée] micro toujours indisponible après reset dur : {e2}")
                    if _trace: _trace.fail("start_retry", e2)
                    # Repli 1 : micro intégré, résolu dans le worker (process sain).
                    if _time_left(_budget_dl) > 0.05:
                        try:
                            ok = self.engine.start_recording(prefer="builtin",
                                                             deadline=_budget_dl)
                            if ok:
                                log.info("[dictée] micro récupéré sur le micro intégré (worker).")
                                _via = "builtin"
                        except Exception as e3:
                            log.debug("engine.start_recording : échec ignoré", exc_info=True)
                            if _trace: _trace.fail("fallback_builtin", e3)
                    # Repli 2 (dernier recours) : autres micros, par NOM, listés
                    # et résolus par le worker (v1.5), tant que le budget tient.
                    if not ok:
                        for _dev in (self.engine.list_input_devices() or [])[:2]:
                            if _time_left(_budget_dl) <= 0.05:
                                break
                            try:
                                ok = self.engine.start_recording(device=_dev,
                                                                 deadline=_budget_dl)
                            except Exception:
                                log.debug("engine.start_recording : échec ignoré", exc_info=True)
                                continue
                            if ok:
                                log.info(f"[dictée] micro récupéré sur le périphérique {_dev}.")
                                _via, _dev_ok = "device", _dev
                                break
                        if not ok and _trace:
                            _trace.fail("fallback_exhausted")
                if _trace: _trace.log_outcome(ok, _via, device=_dev_ok)
                if not ok:
                    _mic_fail_count[0] += 1
                    _grab = _mic_grabber_name()
                    # v1.0.22 — RÉPARATION EN ARRIÈRE-PLAN : on a rendu la main
                    # dans le budget, mais on ne laisse pas le micro cassé. Un
                    # worker NEUF est préparé pendant que l'utilisateur lit le
                    # message ; le terrain montre qu'il réappuie sous ~3 s
                    # (médiane mesurée) et il retombera alors sur un micro sain.
                    # v1.5 : cette réparation cède la place à un appui qui
                    # survient pendant qu'elle tourne (l'appui rejoint son spawn).
                    self._repair_mic_in_background()
                    # v1.0.21 — DIAGNOSTIC HONNÊTE. On vient d'essayer la ré-init PROFONDE
                    # PUIS le micro intégré EXPLICITEMENT. Si ça échoue ENCORE, une app
                    # « simplement lancée » (OBS en fond, etc.) n'est presque jamais la
                    # vraie cause : macOS ne verrouille pas le micro intégré à une app
                    # tierce. C'est le sous-système audio qui est figé. On ARRÊTE donc
                    # d'accuser OBS/Zoom à tort (plainte terrain : « ça me met qu'OBS est
                    # ouvert, beaucoup de fois ») et on donne le bon diagnostic.
                    if _mic_fail_count[0] >= 3:
                        # v1.0.22 — le « redémarrez votre Mac » ne tombe plus au
                        # 2e échec : avec le worker jetable, deux échecs d'affilée
                        # sont normaux le temps qu'un process neuf soit prêt. On
                        # ne parle d'impasse système qu'au 3e, et on dit d'abord
                        # la chose utile : réessayer (le micro vient d'être remis à neuf).
                        _toast("Le système audio de macOS semble bloqué. Réessayez "
                               "votre dictée : le micro vient d'être réinitialisé. Si "
                               "ça persiste, fermez les applications qui captent le "
                               "micro (visio, enregistrement).", kind="error")
                    elif _grab:
                        # 1er échec, sous-système sain : un enregistreur PEUT tenir le
                        # micro. On le mentionne au conditionnel (on n'en est pas certain).
                        _toast(_msg(f"{_grab} est peut-être en train d'utiliser le micro. "
                                    "Réglez son micro sur « Aucun » ou fermez-le, puis "
                                    "relancez votre dictée.",
                                    f"{_grab} may be using the microphone. Set its "
                                    "microphone to “None” or quit it, then dictate "
                                    "again."), kind="error")
                    else:
                        _toast("Micro occupé une seconde, on le remet à neuf. "
                               "Réessayez votre dictée tout de suite.", kind="info")
                    self._release_activity()
                    return False
            if not ok:
                self._release_activity()
                return False
            if self._connecting_shown:
                overlay.recording()   # la pilule repasse de « connexion » à la waveform
            _mic_fail_count[0] = 0   # Fix 4 — micro OK : on remet le compteur à zéro
            self._active = True
            # v1.0.22 — _busy n'est PLUS posé au start : il signifie désormais
            # « finalisation(s) en vol » (posé par stop_and_finalize, compteur
            # _fin_inflight). Le start enchaîné ne doit pas le toucher.
            self._gen += 1            # CONC-5 : nouvelle dictée
            self._t0 = time.time()   # horodatage début (durée d'enregistrement)
            # v11 — Aperçu live SUPPRIMÉ : il était lent et faisait perdre confiance.
            # Pendant la dictée, seule la wave anime ; la transcription apparaît
            # avec un effet "reveal mot-à-mot" au stop.
            # v11 — Nouveau worker : pousse le RMS audio à l'UI à 10 Hz pour
            # que la wave "respire" en suivant la voix (sinon c'est juste un sinus
            # déterministe qui ne réagit pas → impression de bug).
            self._audio_worker = threading.Thread(
                target=self._audio_level_loop, args=(self._stop_evt,), daemon=True
            )
            self._audio_worker.start()
            # v21 — ANTI-VARIANCE (trois mesures, sortie texte inchangée) :
            # 1) assertion anti-App Nap le temps de la dictée + transcription
            #    (posée avant l'ouverture, v1.5) ;
            # 2) préchargement ANTICIPÉ des modèles pendant que l'utilisateur
            #    parle (si l'éco-RAM les avait déchargés, le rechargement est
            #    masqué par la parole au lieu de s'ajouter à l'attente), une
            #    fois la capture installée (CAPTURE_SETTLE_S) ;
            # 3) découpeur (livetail.py, modes auto/fidèle) : morceaux
            #    transcrits PENDANT la parole, à chaque vraie pause ; seule la
            #    fin reste à transcrire au relâchement.
            speed = "auto"
            try:
                speed = _load_settings().get("dictation_speed", "auto")
            except Exception:
                log.debug("get : échec ignoré", exc_info=True)
            _prewarm = threading.Timer(
                self.CAPTURE_SETTLE_S,
                lambda: self.engine.prewarm(want_small=(speed in ("auto", "rapide"))))
            _prewarm.daemon = True
            _prewarm.start()
            self._tail = (_DictationLiveTail(self.engine)
                          if speed in ("auto", "fidele") else None)
            return True

    def _dictation_device(self):
        """v1.5 — NOM du micro choisi dans les réglages, appliqué aussi à la
        dictée (avant : réunions seulement). None = micro par défaut : rien de
        choisi, ou un périphérique de capture du son système (inutile ici).
        Absent au moment de l'appui, le worker retombe sur le défaut."""
        name = _load_settings().get("audio_device_name")
        if not name or any(k in name.lower() for k in _LOOPBACK_KW):
            return None
        return name

    def _mic_connecting(self):
        """Ouverture lente annoncée par le worker (Bluetooth, bascule HFP) :
        la pilule le dit au lieu de laisser croire à une dictée en cours."""
        self._connecting_shown = True
        overlay.info(_te("Connexion du micro"), _te("Un instant, le micro se met en route."))

    def _release_activity(self):
        """Fin de l'assertion anti-App Nap d'un appui qui n'a pas abouti."""
        tok, self._activity = self._activity, None
        if tok is not None:
            _end_activity(tok)

    def _repair_mic_in_background(self):
        """Worker micro neuf préparé EN FOND pour l'appui suivant (il cède la
        place à un appui qui survient pendant qu'il tourne)."""
        threading.Thread(target=lambda: self.engine.hard_mic_reset(timeout=5.0),
                         name="mic-repair-bg", daemon=True).start()

    def _after_capture(self, stop_evt):
        """v1.5 — Bilan de la capture qui vient de se fermer, UNE fois par dictée :
          - dépassements (blocs perdus par CoreAudio ou par la file du worker) :
            journal + un incident mic_overflow ;
          - flux mort pendant la dictée : worker neuf préparé tout de suite, au
            repos, plutôt qu'au prochain appui."""
        st = getattr(self.engine, "last_capture_stats", None) or {}
        xruns, dropped = int(st.get("xruns", 0)), int(st.get("dropped", 0))
        if xruns or dropped:
            log.warning(f"[dictée] capture dégradée : {xruns} dépassement(s) CoreAudio, "
                  f"{dropped} bloc(s) perdu(s) dans la file du worker.")
            if _EV:
                _EV.log(_EV.E.MIC_OVERFLOW, level="warning", xruns=xruns, dropped=dropped)
        if stop_evt.stream_dead:
            self._mic_lost = False
            self._repair_mic_in_background()

    def _audio_level_loop(self, stop_evt=None):
        """Pousse le RMS du buffer audio à l'UI à 10 Hz pendant la dictée.
        Permet à la wave UI de suivre la voix en temps réel.
        v1.0.22 — `stop_evt` = l'event de CETTE dictée (chaînage : self._stop_evt
        peut déjà appartenir à la dictée suivante au moment où on le lirait)."""
        stop_evt = stop_evt if stop_evt is not None else self._stop_evt
        _warned_no_audio = False
        while not stop_evt.is_set():
            stop_evt.wait(0.1)  # 10 Hz
            if stop_evt.is_set():
                break
            try:
                rms = self.engine.rms_recent(0.06)
                _ui("setAudioLevel(%.4f)" % rms)
                overlay.level(rms)   # alimente la waveform de l'overlay (no-op si absent)
                # v1.0.11 — DÉTECTEUR DE FLUX MORT : si plus aucun échantillon micro
                # n'arrive depuis >3 s (débranché, sortie de veille, casque BT coupé),
                # on PRÉVIENT l'utilisateur (une seule fois) au lieu de le laisser
                # dicter dans le vide, et on marque le micro pour ré-init profonde au
                # prochain démarrage. SILENCE normale = l'écart n'augmente pas (le
                # callback livre des échantillons silencieux) -> zéro faux positif.
                if not _warned_no_audio and self.engine.seconds_since_audio() > 3.0:
                    _warned_no_audio = True
                    self._mic_lost = True
                    stop_evt.stream_dead = True
                    if _EV: _EV.log(_EV.E.MIC_DEVICE_FAIL, where="stream_dead")
                    try:
                        overlay.info(_te("Aucun son"),
                                     _te("Micro déconnecté ? Relâchez et réessayez."))
                    except Exception:
                        log.debug("overlay.info : échec ignoré", exc_info=True)
            except Exception:
                log.debug("engine.rms_recent : échec ignoré", exc_info=True)
                continue

    def cancel(self) -> bool:
        """v2 — ANNULE la dictée en cours : stoppe les workers, JETTE l'audio,
        AUCUNE transcription. Pour la touche Échap / bouton Annuler. L'utilisateur
        qui veut juste arrêter (dictée de 5 min) n'attend rien. False si rien à
        annuler. Idempotent et thread-safe (verrou _lock comme start/stop)."""
        with self._lock:
            if not self._active:
                return False
            self._active = False
            # v1.0.22 — on ne touche PLUS _busy ici : il signifie désormais
            # « finalisation(s) en vol » (chaînage) — annuler la capture en
            # cours ne doit pas masquer une transcription précédente au
            # superviseur. Le compteur _fin_inflight fait foi.
        # Stoppe le worker de niveau audio avant de jeter l'audio.
        self._stop_evt.set()
        if self._audio_worker is not None:
            self._audio_worker.join(timeout=2.0)
            self._audio_worker = None
        # v21 — live-tail + assertion : tout relâcher (annulation = rien à garder).
        tail = getattr(self, "_tail", None)
        if tail is not None:
            tail.abort()
            self._tail = None
        _end_activity(getattr(self, "_activity", None))
        self._activity = None
        # Ferme le micro et JETTE l'audio — pas de transcription.
        try:
            self.engine.cancel_recording()
        except Exception:
            log.debug("engine.cancel_recording : échec ignoré", exc_info=True)
        return True

    def force_reset(self, reason: str = "") -> None:
        """v1.0.9 — AUTO-HEAL : ramène la dictée à un état PRÊT en une fraction de
        seconde, même si un thread est coincé dans le sous-système audio. N'attend
        AUCUN verrou (assignations atomiques sous le GIL) -> ne peut pas se bloquer
        à son tour. Bumpe `_gen` (toute finalisation en vol devient périmée et
        n'insère plus rien), coupe le worker audio, et REMET LE MICRO À NEUF
        (engine.reset_audio, fermeture bornée). Le prochain raccourci repart sain.
        Idempotent. Piloté par le superviseur de dictée et le reset manuel."""
        log.info(f"[auto-heal] reset dictée ({reason}).")
        # 1) état -> libre immédiatement (un démarrage ultérieur passe)
        self._active = False
        self._busy = False
        self._busy_since = 0.0
        try:
            self._gen = int(getattr(self, "_gen", 0)) + 1
        except Exception:
            log.debug("int : échec ignoré", exc_info=True)
            self._gen = 0
        # v1.0.22 — chaînage : invalide TOUTES les finalisations en vol (epoch :
        # texte périmé jamais inséré, attentes FIFO abandonnées) et remet la
        # file à zéro (aucun waiter ne reste suspendu). Assignations d'abord
        # (atomiques sous le GIL, jamais bloquantes), notification best-effort.
        self._epoch = int(getattr(self, "_epoch", 0)) + 1
        self._fin_inflight = 0
        self._fin_serving = int(getattr(self, "_fin_next", 0))
        try:
            with self._fin_cv:
                self._fin_cv.notify_all()
        except Exception:
            log.debug("_fin_cv.notify_all : échec ignoré", exc_info=True)
        # 2) coupe le worker de niveau audio + relâche live-tail / assertion App Nap
        try:
            self._stop_evt.set()
        except Exception:
            log.debug("_stop_evt.set : échec ignoré", exc_info=True)
        self._audio_worker = None
        try:
            tail = getattr(self, "_tail", None)
            if tail is not None:
                tail.abort()
        except Exception:
            log.debug("getattr : échec ignoré", exc_info=True)
        self._tail = None
        try:
            _end_activity(getattr(self, "_activity", None))
        except Exception:
            log.debug("_end_activity : échec ignoré", exc_info=True)
        self._activity = None
        # 3) MICRO À NEUF : abandonne un flux figé, repart propre (jamais bloquant)
        try:
            self.engine.reset_audio()
        except Exception:
            log.debug("engine.reset_audio : échec ignoré", exc_info=True)

    def empty_overlay_text(self):
        """Texte court (FR, à passer par _te) qui explique pourquoi la dernière
        finalisation lancée par CE thread n'a rendu aucun texte ; None si elle
        n'a rien précisé."""
        return getattr(self._outcome, "overlay", None)

    def stop_and_finalize(self, notify_ui: bool = True) -> str:
        """Arrête tout, produit le texte FINAL propre, le copie, renvoie le texte.
        notify_ui=False : ne touche PAS la fenêtre principale (chemin raccourci ->
        seul l'overlay s'anime, pas de « double »).
        Le verrou anti-chevauchement (_busy) est TOUJOURS libéré à la fin (finally),
        même si la transcription lève une exception -> jamais bloqué « occupé »."""
        # v1.0.22 — ACQUISITION BORNÉE : si un start() est coincé dans l'escalade
        # micro (verrou tenu), le relâchement ne doit PAS attendre derrière lui —
        # c'était le scénario « je parle, je relâche, rien ne se passe, je dois
        # relancer l'appli ». Au-delà de 2 s, on déclenche l'auto-réparation.
        self._outcome.overlay = None
        if not self._lock.acquire(timeout=2.0):
            log.warning("[dictée] verrou de démarrage figé -> auto-réparation forcée.")
            if _EV: _EV.log(_EV.E.MIC_DEVICE_FAIL, where="start_lock_stuck")
            threading.Thread(target=self.force_reset, args=("verrou start figé",),
                             name="heal-lock", daemon=True).start()
            return ""
        try:
            if not self._active:
                return ""
            self._active = False
            # v1.0.22 — CHAÎNAGE : on DÉTACHE l'état de CETTE dictée (worker de
            # niveau, live-tail, assertion App Nap, event d'arrêt) : une dictée
            # suivante peut démarrer pendant cette finalisation et poser les
            # SIENS — le teardown ci-dessous ne doit toucher que les nôtres.
            _my_evt = self._stop_evt
            _my_worker = self._audio_worker
            self._audio_worker = None
            _my_tail = self._tail
            self._tail = None
            _my_act = self._activity
            self._activity = None
            # v1.1.0 : durée d'audio de CETTE dictée, pour dimensionner le budget
            # de finalisation (_t0 peut appartenir à la suivante après le release).
            _my_dur = max(0.0, time.time() - float(getattr(self, "_t0", 0.0) or time.time()))
        finally:
            self._lock.release()
        # v1.0.22 — ticket FIFO (ordre de transcription ET d'insertion garanti
        # entre dictées enchaînées) + compteur « en vol » (_busy dérivé).
        with self._fin_cv:
            _my_ticket = self._fin_next
            self._fin_next += 1
            self._fin_inflight += 1
            _my_epoch = self._epoch
            self._busy = True
            self._busy_since = time.time()
        self._finalize_t0 = time.time()   # v1.0.9 : départ de la finalisation (horloge superviseur)
        # v1.0.22 — BATTEMENT DE CŒUR : horodatage rafraîchi à chaque étape de la
        # finalisation. Le superviseur mesure désormais l'INACTIVITÉ, pas la
        # durée totale. Avant, son plafond (9-12 s) tombait SOUS les bornes
        # réelles du moteur (MLX 22 s) : une transcription lente mais SAINE
        # était déclarée « coincée », l'epoch bumpé, et le texte — qui finissait
        # par arriver — n'était JAMAIS inséré au curseur, avec en prime un
        # message « Dictée réparée » trompeur.
        self._fin_beat = time.time()
        # v29.2 — WATCHDOG DE FINALISATION (anticipe TOUTE la classe « dictée
        # infinie ») : la finalisation peut se figer AILLEURS que dans le worker
        # GPU (fusion, verrou, attente disque...). Si la dictée ne rend pas la
        # main dans son budget, on RÉCUPÈRE de force : libère _busy, avance la
        # file FIFO et prévient l'utilisateur ; le résultat tardif n'est jamais
        # inséré (voir _late_dictation). v1.4.4 : armé au TOUR de la dictée.
        _wd_s = _finalize_budget_s(_my_dur)   # v1.1.0 : proportionnel à l'audio

        def _on_fire():
            # v1.0.22 — chaînage : libère le compteur + AVANCE la file FIFO
            # (une finalisation figée ne doit jamais retenir la suivante).
            with self._fin_cv:
                self._release_inflight(_my_epoch)
                self._fin_serving = max(self._fin_serving, _my_ticket + 1)
                self._busy = self._fin_inflight > 0
                self._fin_cv.notify_all()
            _end_activity(_my_act)
            log.warning(f"[dictée] finalisation FIGÉE > {_wd_s:.0f}s -> "
                  "récupération forcée (watchdog), dictée non insérée.")
            if _EV: _EV.log(_EV.E.FINALIZE_TIMEOUT, s=round(_wd_s), audio_s=round(_my_dur, 1))
            toast_msg, overlay_msg = _late_dictation_messages()
            try:
                overlay.error(_te(overlay_msg))
                _schedule_overlay_hide(4.0)
            except Exception as e_ov:
                log.warning(f"[dictée] overlay indisponible : {e_ov}")
            _toast(toast_msg, kind="error")

        def _on_beat():
            self._fin_beat = time.time()

        # v1.0.22 — BATTEUR BORNÉ. La transcription est un appel ATOMIQUE (GPU) :
        # sans battement pendant cette étape, le superviseur couperait à 9 s une
        # transcription parfaitement saine. Le batteur couvre l'attente du tour
        # puis le budget, et PAS au-delà : l'auto-réparation garde ses droits
        # sur une VRAIE panne.
        watch = _FinalizeWatch(self._fin_cv, _wd_s, _on_fire, _on_beat)
        watch.start()
        try:
            return self._do_finalize(notify_ui, _my_evt, _my_worker, _my_tail,
                                     _my_ticket, _my_epoch, watch)
        finally:
            watch.disarm()
            # v1.0.22 — rendre le ticket UNE seule fois : si le watchdog l'a déjà
            # rendu (finalisation qui se réveille tardivement), on n'avance que
            # la file (max idempotent), sans re-décrémenter le compteur.
            with self._fin_cv:
                if not watch.fired:
                    self._release_inflight(_my_epoch)
                self._fin_serving = max(self._fin_serving, _my_ticket + 1)
                self._busy = self._fin_inflight > 0
                self._fin_cv.notify_all()
            # v21 — fin de l'assertion anti-App Nap (succès OU échec).
            _end_activity(_my_act)

    def _release_inflight(self, epoch: int) -> None:
        """Rend la place « en vol » d'une finalisation (sous _fin_cv). v1.5 —
        une finalisation d'avant force_reset n'est plus comptée (le reset a
        remis le compteur à zéro) : la décompter retirait la place d'une
        dictée lancée depuis, et _busy tombait pendant sa transcription."""
        if epoch == self._epoch:
            self._fin_inflight = max(0, self._fin_inflight - 1)

    def _late_dictation(self, raw: str) -> str:
        """v1.4.4 — Résultat arrivé APRÈS le chien de garde : l'utilisateur a déjà
        lu que la dictée ne serait pas insérée. On ne perd pas le texte, mais on
        ne touche à rien de ce qu'il fait maintenant : ni curseur, ni
        presse-papier, ni compteurs, ni rappels. Le texte va à l'historique (si
        l'utilisateur le garde) et on le lui dit. Aucun incident de plus :
        finalize_timeout a déjà compté cet appui. Rend toujours ""."""
        raw = _polish_dictation(raw)
        if not (raw and _history_keeps_dictations()):
            log.info("[dictée] résultat tardif vide ou historique désactivé -> rien à garder.")
            self._outcome.overlay = _late_dictation_messages()[1]
            return ""
        try:
            appstate.store.add_dictation(raw, mode="DICTEE")
        except Exception as e:
            log.warning(f"[dictée] dictée tardive non sauvegardée : {e}")
            self._outcome.overlay = _LOST_DICTATION_OVERLAY
            _toast(_LOST_DICTATION_TOAST, kind="error")
            return ""
        log.info("[dictée] résultat tardif rangé dans l'historique (non inséré).")
        _refresh_lists_ui()
        self._outcome.overlay = _LATE_SAVED_OVERLAY
        _toast(_LATE_SAVED_TOAST, kind="info")
        return ""

    def _do_finalize(self, notify_ui: bool, my_evt, my_worker, my_tail,
                     my_ticket: int, my_epoch: int, watch: "_FinalizeWatch") -> str:
        # v1.0.22 — chaînage : on opère sur l'état DÉTACHÉ de CETTE dictée
        # (my_evt/my_worker/my_tail, détachés sous verrou par stop_and_finalize) —
        # les champs self.* peuvent déjà appartenir à la dictée suivante.
        # Signale l'arrêt au worker de niveau audio (le NÔTRE).
        my_evt = my_evt or self._stop_evt
        my_evt.set()
        # v13.1 — On joint le worker audio_level (sinon thread zombie
        # qui continue à appeler _ui("setAudioLevel(...)") jusqu'à l'extinction
        # du process, et qui se cumule avec le worker de la dictée suivante
        # — c'est ce qui causait la latence croissante d'enregistrement en
        # enregistrement).
        if my_worker is not None:
            my_worker.join(timeout=2.0)

        # Transcription FINALE (vad on) + assemblage propre des espaces.
        # Vlocal 2 — Vitesse de dictée :
        #   auto   : CASCADE INTELLIGENTE tunée — small si l'audio est net
        #            (~1,3 s, 2,6x plus rapide, fidélité quasi identique sur le
        #            français courant), turbo au moindre doute GLOBAL (seuils
        #            calibrés sur la confiance réelle : fraction de mots <0,3,
        #            pas un seul mot comme l'ancienne cascade cassée). Le
        #            glossaire corrige les termes connus quel que soit le modèle.
        #   rapide : small forcé (vitesse max).
        #   fidele : turbo direct (fidélité max, plancher ~3,5 s).
        speed = "auto"
        try:
            speed = _load_settings().get("dictation_speed", "auto")
        except Exception:
            log.debug("get : échec ignoré", exc_info=True)
        # v21 — QoS USER_INITIATED sur CE thread (et le décodage qu'il pilote) :
        # pas de démotion E-core/background pendant que l'utilisateur attend.
        from engine import _qos_user_initiated
        _qos_user_initiated()
        # v21 — live-tail : récupère les fenêtres déjà transcrites pendant la
        # parole (dictée longue). ([], 0) si la dictée était courte -> chemin
        # actuel inchangé.
        # v1.0.22 — ORDRE CORRIGÉ : on FERME LA CAPTURE D'ABORD (worker micro,
        # borné) -> le créneau est LIBRE pour la dictée suivante en ~0,3 s après
        # le relâchement, et la pastille micro s'éteint tout de suite. Avant, la
        # collecte du live-tail (join jusqu'à 20 s) passait AVANT la fermeture :
        # sur une dictée longue, le micro restait ouvert et la dictée suivante
        # était refusée pendant tout ce temps — la promesse d'enchaînement
        # tombait précisément là où elle est le plus utile.
        audio = self.engine.stop_recording()
        self._after_capture(my_evt)
        self._fin_beat = time.time()      # v1.0.22 : battement (capture fermée)
        lt = None
        tail = my_tail
        if tail is not None:
            # v1.7 : le découpeur reçoit l'audio complet ; il interrompt tout
            # calcul de fond qui ne rend pas le texte final.
            texts, tpos = tail.stop_and_collect(audio)
            if texts and tpos > 0:
                lt = (texts, tpos)
        self._fin_beat = time.time()      # v1.0.22 : battement (live-tail collecté)
        # Durée mesurée sur l'AUDIO (self._t0 peut déjà appartenir à la dictée
        # suivante en cas d'enchaînement).
        _rec_dur = float(len(audio)) / max(1, int(self.engine.sample_rate))
        # v1.0.22 — ORDRE GARANTI : les dictées enchaînées se transcrivent et
        # s'insèrent dans l'ordre des relâchements (tickets FIFO). Attente
        # BORNÉE (le watchdog/la fin du prédécesseur avance toujours la file) ;
        # un force_reset (epoch) invalide l'attente -> abandon propre.
        with self._fin_cv:
            _deadline = time.time() + _finalize_budget_s(_rec_dur) + 5.0
            while (self._fin_serving < my_ticket
                   and self._epoch == my_epoch
                   and time.time() < _deadline):
                self._fin_cv.wait(0.25)
            if self._epoch != my_epoch:
                log.info("[dictée] finalisation invalidée par un reset -> abandon.")
                return ""
        watch.arm()   # v1.4.4 : notre tour est venu, le budget commence ici
        _tt0 = time.perf_counter()
        raw, stalled, crash = "", None, None
        try:
            raw = self.engine.stop_and_transcribe(
                fast=(speed == "rapide"), adaptive=(speed == "auto"),
                live_tail=lt, audio=audio)
        except TimeoutError as e:
            stalled = e
        except Exception as e:
            log.debug("engine.stop_and_transcribe : échec ignoré", exc_info=True)
            crash = e
        # v1.4.4 — UN verdict par appui : si le chien de garde a déjà parlé, ni
        # incident, ni message, ni insertion de plus.
        if not watch.claim():
            return self._late_dictation(raw)
        if stalled is not None:
            # v29.1 — figeage GPU rare sous dictée intensive : mlx_engine a borné
            # l'inférence (60 s) et RÉINITIALISÉ le worker. On ne bloque pas : le
            # verrou _busy est libéré par le finally de stop_and_finalize, et la
            # dictée suivante repart sur un worker neuf (fini le blocage 180 s).
            log.warning(f"[dictée] transcription interrompue ({stalled}).")
            if _EV: _EV.log(_EV.E.GPU_TIMEOUT)
            # v29.2 — et on NETTOIE l'overlay (sinon il restait coincé en
            # « transcription… » et chevauchait l'UI -> panneau fantôme).
            # La carte d'erreur de la pilule ajoute d'elle-même l'invitation à
            # réessayer (« Réessayez : maintenez … ») : le titre ne la répète pas.
            self._outcome.overlay = "La transcription a calé."
            try:
                overlay.error(_te("La transcription a calé."))
                _schedule_overlay_hide(3.0)
            except Exception:
                log.debug("overlay.error : échec ignoré", exc_info=True)
            _toast("La transcription a calé un instant (GPU), c'est réinitialisé. "
                   "Relancez votre dictée, ça repart.", kind="error")
            return ""
        # v21 — ligne de DIAGNOSTIC par dictée (décomposition de la variance) :
        # visible dans vlocal.log pour vérifier la stabilité en conditions réelles.
        log.info(f"[diag] dictée {_rec_dur:.1f}s -> transcription "
              f"{time.perf_counter() - _tt0:.2f}s "
              f"(branche {getattr(self.engine, 'last_model', '?')}"
              f"{', live-tail ' + str(len(lt[0])) + ' fen.' if lt else ''}"
              f"{'; découpeur ' + str(tail.stats) if tail is not None else ''})")

        # --- CHEMIN BRUT : instantané, prioritaire ---
        # Le texte brut nettoyé EST le produit garanti : copié IMMÉDIATEMENT.
        # GARDE-FOU : aucun post-traitement LLM ici — le reformatage SLM a été
        # supprimé en v15, le brut est le seul produit.
        # v9 TOP-5 #2 / v11 — Feedback explicite micro non autorisé / silence.
        # Aucun emoji (cf. brief V11). Texte clair sans jargon.
        if crash is not None:
            log.warning(f"[dictée] transcription en échec : {crash}")
        if not raw:
            # v1.4.4 — texte vide : on dit POURQUOI, d'après le moteur
            # (engine.last_empty_reason) et le signal capté. Avant, tout ce
            # qui n'était pas un signal nul devenait « silence », panne moteur
            # comprise, avec un « son très faible » qui mentait. C'est ICI, et
            # nulle part ailleurs, que transcribe_fail est journalisé.
            rms = float(getattr(self.engine, "last_rms", 0.0))
            peak = float(getattr(self.engine, "last_peak", 0.0))
            reason = "fail" if crash is not None else self.engine.last_empty_reason()
            code = (_EV.empty_text_code(reason, rms, peak) if _EV
                    else ("transcribe_fail" if reason == "fail" else "silence"))
            # Un flux mort a déjà été journalisé pendant l'enregistrement
            # (mic_device_fail:stream_dead) : même appui, même cause, un seul incident.
            if _EV and not (code == _EV.E.MIC_ZERO_SIGNAL and my_evt.stream_dead):
                _EV.log(code, reason=reason, rms=round(rms, 5), peak=round(peak, 5),
                        error=type(crash).__name__ if crash is not None else None)
            toast_msg, self._outcome.overlay = _EMPTY_TEXT_MSG[code]
            _toast(toast_msg, kind="error")
            return ""

        raw = _polish_dictation(raw)
        if not raw:
            return ""   # hallucination pure scrubée -> rien à insérer (mieux que du faux)

        # v29.10 — la copie EST le produit (collage Cmd+V). Si elle échoue (rare),
        # on NE ment PAS avec flashCopied : on prévient clairement, sinon le texte
        # serait perdu en silence. Le chemin SUCCÈS est INCHANGÉ (= dictée validée).
        _copy_ok = copy_to_clipboard(raw)
        if _copy_ok:
            if notify_ui:
                _ui("flashCopied()")
            # v1.0.18 — Fix 2 : si l'Accessibilité est OFF, le texte est copié mais
            # PAS inséré automatiquement au curseur. On le signale UNE SEULE FOIS
            # (jamais de spam), sans rien changer au repli presse-papier (= produit).
            try:
                if notify_ui and not _acc_warned[0] \
                        and not permissions.accessibility_ok():
                    _acc_warned[0] = True
                    _toast("Texte copié : collez avec Cmd+V. Pour l'insertion "
                           "automatique au curseur, autorisez Vlocal dans Réglages "
                           "Système > Confidentialité et sécurité > Accessibilité.",
                           kind="info")
            except Exception:
                log.debug("_toast : échec ignoré", exc_info=True)
        else:
            log.warning("[dictee] copie presse-papier ÉCHOUÉE -> texte non collable.")
            if _EV: _EV.log(_EV.E.CLIPBOARD_FAIL)
            _toast("Le texte n'a pas pu être copié dans le presse-papier. "
                   "Réessayez votre dictée.", "error")

        # v1.1.0 — compteur d'usage (dictées, mots, temps gagné) : aucun contenu,
        # alimenté quel que soit le réglage d'historique. Sert l'accueil et la
        # télémétrie déclarée (telemetry.py), qui est prévenue sans bloquer.
        try:
            if appstate.store is not None and raw:
                appstate.store.record_usage(appstate.store.word_count(raw), _rec_dur)
                # v1.2.0 — horodatage de la DERNIÈRE dictée : c'est lui qui est
                # partagé (si l'utilisateur l'accepte), pas l'heure d'envoi ;
                # la console peut donc dire « utilisé il y a 12 minutes » juste.
                _save_settings({"last_used_at": time.time()})
                if appstate.telemetry is not None:
                    appstate.telemetry.notify_usage()
        except Exception:
            log.debug("store.record_usage : échec ignoré", exc_info=True)
        # v18.4 — Historique des DICTÉES (texte seul, très léger). Persisté
        # UNIQUEMENT si le réglage de stockage = "all" (les rappels sont déjà
        # rangés dans leur table). Jamais d'audio pour une dictée.
        try:
            # v1.0.7 — un VRAI rappel daté est rangé dans Rappels, PAS aussi en
            # dictée (fini le doublon « rappel + dictée » remonté par Raphael).
            if _current_mode() == "DICTEE" and _history_keeps_dictations() \
                    and not _looks_like_timed_reminder(raw):
                appstate.store.add_dictation(raw, mode="DICTEE")
                # Bug #4 — rafraîchit l'accueil (mots / temps gagné / sessions du
                # mois) après une dictée, comme le fait déjà le chemin réunion.
                # Non bloquant (via _ui fire-and-forget), hors chemin de copie,
                # zéro impact latence/RAM.
                _refresh_lists_ui()
        except Exception as e:
            log.warning(f"[dictee] historique non sauvegardé (sans gravité) : {e}")

        # --- POST-PROCESS (asynchrone, hors chemin brut) ------------------
        # v2 — En DICTÉE, on lance l'AUTO-DÉTECTION de rappel (« rappelle-moi… »
        # -> rappel créé). _post_process_by_mode est sans effet en RÉUNION.
        # Le texte brut est déjà copié (chemin presse-papier instantané).
        threading.Thread(
            target=_post_process_by_mode,
            args=(raw, _current_mode()),
            daemon=True,
        ).start()
        return raw
