#!/usr/bin/env python3
"""
Vlocal — politique mémoire des moteurs de transcription (v1.6).

AVANT : minuteries fixes. Le modèle GPU était déchargé après 60 à 150 s
d'inactivité selon la RAM « disponible », même sur une machine à l'aise ; la
dictée suivante payait le rechargement (0,08 s cache disque chaud, plusieurs
secondes quand macOS a évincé les poids du cache) et chaque cycle
décharge/recharge ajoutait une transition Metal (cause prouvée de gels).

MAINTENANT : on décharge SOUS PRESSION MÉMOIRE RÉELLE, dans un ordre fixe :
  1. le modèle CPU (turbo + small), qui n'est qu'un REPLI quand le GPU marche ;
  2. puis le modèle GPU, seulement si la pression persiste.
Le cache de tampons MLX n'a pas de palier propre : il est déjà rendu après
CHAQUE inférence (mlx_engine, mesuré 0 Mo retenu, coût nul en latence).
Sans pression, rien n'est déchargé, sauf le modèle CPU de REPLI resté inutilisé
CPU_FALLBACK_IDLE_S (le GPU est revenu : 1,7 Go qui ne servent plus).

Signal de pression : le niveau du noyau (kern.memorystatus_vm_pressure_level :
1 normal, 2 avertissement, 4 critique), le même que celui des notifications
dispatch de macOS, lu par sysctlbyname (quelques microsecondes, aucun
processus lancé). Garde-fou : la mémoire disponible (psutil), si le noyau
tarde à signaler. Le rechargement reste masqué : le préchargement au DÉBUT de
la parole (VlocalEngine.prewarm) recharge le modèle pendant que l'on parle.

Module pur et testable : `plan()` ne fait qu'une décision à partir d'un état ;
l'app exécute les actions (app._start_idle_unloader).
"""
import ctypes
import ctypes.util

NORMAL, WARN, CRITICAL = 0, 1, 2
LEVEL_NAMES = {NORMAL: "normale", WARN: "avertissement", CRITICAL: "critique"}

# Niveaux du noyau (sys/kern_memorystatus.h) -> nos niveaux.
_KERNEL_LEVELS = {1: NORMAL, 2: WARN, 4: CRITICAL}

# Garde-fou sur la mémoire disponible (Go). psutil sur-estime un peu sous
# macOS (pages compressibles) : seuils bas, le noyau reste le signal principal.
AVAIL_WARN_GB = 1.0
AVAIL_CRITICAL_GB = 0.5

# Délai de grâce sous AVERTISSEMENT : on ne décharge pas un modèle utilisé il y
# a moins de 30 s (dictées en rafale). Sous pression CRITIQUE : aucun délai.
PRESSURE_GRACE_S = 30.0
# Modèle CPU de repli inutilisé depuis 5 min alors que le GPU fonctionne.
CPU_FALLBACK_IDLE_S = 300.0

# Préchargement du repli CPU au premier gel GPU : pic RSS mesuré du turbo int8
# 2,25 Go (M5, faster-whisper) + 1 Go laissé au système. En dessous, on ne
# précharge pas : le chargement paresseux reste possible si une dictée en a
# besoin, mais on n'impose pas 2,2 Go à une machine déjà serrée.
CPU_PRELOAD_MIN_AVAIL_GB = 3.3


_libc = None


def _sysctl_int(name: str):
    """Valeur entière d'un sysctl, ou None si illisible (non-macOS, nom absent)."""
    global _libc
    try:
        if _libc is None:
            _libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        libc = _libc
        val = ctypes.c_int(0)
        size = ctypes.c_size_t(ctypes.sizeof(val))
        if libc.sysctlbyname(name.encode(), ctypes.byref(val), ctypes.byref(size),
                             None, ctypes.c_size_t(0)) != 0:
            return None
        return val.value
    except (OSError, AttributeError, TypeError):
        return None


def available_gb():
    """Mémoire disponible (Go) selon psutil, ou None si indéterminable."""
    try:
        import psutil
        return psutil.virtual_memory().available / 2 ** 30
    except (ImportError, OSError):
        return None


def kernel_level():
    """Niveau de pression du noyau macOS (NORMAL/WARN/CRITICAL) ou None."""
    raw = _sysctl_int("kern.memorystatus_vm_pressure_level")
    return _KERNEL_LEVELS.get(raw) if raw is not None else None


def combine(kernel, avail_gb) -> int:
    """Niveau retenu : le plus sévère des deux signaux (absents = normal)."""
    level = kernel if kernel is not None else NORMAL
    if avail_gb is not None:
        if avail_gb < AVAIL_CRITICAL_GB:
            level = max(level, CRITICAL)
        elif avail_gb < AVAIL_WARN_GB:
            level = max(level, WARN)
    return level


def pressure_level() -> int:
    return combine(kernel_level(), available_gb())


def cpu_preload_allowed(level=None, avail_gb=None) -> bool:
    """Le préchargement du repli CPU (2,2 Go) est-il raisonnable maintenant ?"""
    level = pressure_level() if level is None else level
    avail_gb = available_gb() if avail_gb is None else avail_gb
    if level != NORMAL:
        return False
    return avail_gb is None or avail_gb >= CPU_PRELOAD_MIN_AVAIL_GB


def plan(level: int, *, busy: bool, gpu_loaded: bool, gpu_idle_s: float,
         cpu_loaded: bool, cpu_idle_s: float, cpu_is_fallback: bool) -> tuple:
    """Actions de déchargement à exécuter maintenant, dans l'ordre : "cpu"
    (turbo + small) puis "gpu". Jamais pendant un travail en cours (dictée,
    réunion, import). Sous AVERTISSEMENT, un seul modèle par passage, le CPU
    d'abord : le GPU ne part qu'au passage suivant si la pression persiste."""
    if busy:
        return ()
    if level == CRITICAL:
        return tuple(a for a, on in (("cpu", cpu_loaded), ("gpu", gpu_loaded)) if on)
    if level == WARN:
        if cpu_loaded:
            return ("cpu",) if cpu_idle_s >= PRESSURE_GRACE_S else ()
        if gpu_loaded and gpu_idle_s >= PRESSURE_GRACE_S:
            return ("gpu",)
        return ()
    if cpu_loaded and cpu_is_fallback and cpu_idle_s >= CPU_FALLBACK_IDLE_S:
        return ("cpu",)
    return ()
