"""obsidian.py - Connecteur Obsidian local pour Vlocal (v1.0.13, 100% ADDITIF).

Module PUR (stdlib uniquement, aucun reseau). Sur le modele de reminders.py.
Trois responsabilites :

  1. detect_vault()         trouve le coffre Obsidian de l'utilisateur en lisant
                            le fichier de config qu'Obsidian maintient lui-meme.
                            INDEPENDANT de la version d'Obsidian installee.
  2. route(text, rules)     routeur DETERMINISTE a mots-cles : decide dans quelle
                            note / section ranger une pensee dictee. Zero IA, zero
                            reseau, instantane, explicable.
  3. capture(text, vault)   ecrit la pensee au bon endroit, en local, defensif,
                            ne leve jamais.

Les regles de routage vivent dans <coffre>/.vlocal/routing.json (editable par
l'utilisateur). En l'absence de ce fichier, DEFAULT_ROUTING (generique) s'applique.
Vlocal reste donc generique pour n'importe quel coffre ; la personnalisation vit
dans le coffre. La note du jour est ecrite EXACTEMENT la ou Obsidian l'attend (on
lit la config Daily Notes du coffre), donc compatible avec n'importe quelle config.
"""

import os
import re
import json
import threading
import datetime as _dt

# Verrou d'ecriture : deux dictees rapprochees finalisent sur deux threads daemon
# distincts ; on serialise les read-modify-write d'une meme note.
_WRITE_LOCK = threading.Lock()

_OBSIDIAN_CONFIG = os.path.expanduser(
    "~/Library/Application Support/obsidian/obsidian.json")

# Sentinelle : "ranger dans la note du jour" (resolue via la config du coffre).
DAILY = "__daily__"

# Routage generique par defaut (tout coffre). La personnalisation de Raphael vit
# dans <coffre>/.vlocal/routing.json (projets Captiv/Vlocal/Neolife, etc.).
DEFAULT_ROUTING = {
    "version": 1,
    "default": {"note": DAILY, "section": "## 💡 Capture"},
    "buckets": [
        {
            "name": "Taches",
            "note": DAILY,
            "section": "## 🎯 Focus du jour (1 à 3 max)",
            "as_task": True,
            "keywords": [
                "a faire", "rappelle-moi", "rappelle moi", "rappel", "deadline",
                "todo", "penser a", "ne pas oublier", "il faut que", "noter de",
            ],
        },
    ],
}


# --- Detection du coffre -----------------------------------------------------

def detect_vault():
    """Chemin du coffre Obsidian actif, ou None.

    Lit le fichier que l'app Obsidian maintient (liste des coffres ouverts).
    Choisit le coffre marque 'open', sinon le plus recemment ouvert (ts max)."""
    try:
        with open(_OBSIDIAN_CONFIG, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    vaults = (data or {}).get("vaults") or {}
    best, best_ts = None, -1
    for _id, v in vaults.items():
        if not isinstance(v, dict):
            continue
        path = v.get("path")
        if not path or not os.path.isdir(path):
            continue
        if v.get("open"):
            return path
        ts = v.get("ts") or 0
        if ts > best_ts:
            best_ts, best = ts, path
    return best


def is_vault(path):
    """Un dossier est un coffre Obsidian s'il contient un sous-dossier .obsidian."""
    try:
        return bool(path) and os.path.isdir(os.path.join(path, ".obsidian"))
    except Exception:
        return False


# --- Regles de routage (cote coffre) -----------------------------------------

def load_routing(vault):
    """Lit <coffre>/.vlocal/routing.json, sinon DEFAULT_ROUTING."""
    try:
        p = os.path.join(vault, ".vlocal", "routing.json")
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("buckets"), list):
            d.setdefault("default", DEFAULT_ROUTING["default"])
            return d
    except Exception:
        pass
    return DEFAULT_ROUTING


def seed_routing(vault, routing=None):
    """Ecrit un routing.json par defaut dans le coffre s'il n'existe PAS encore.
    N'ecrase jamais un fichier existant (l'utilisateur peut le personnaliser)."""
    try:
        d = os.path.join(vault, ".vlocal")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "routing.json")
        if not os.path.exists(p):
            with open(p, "w", encoding="utf-8") as f:
                json.dump(routing or DEFAULT_ROUTING, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


# --- Routeur deterministe -----------------------------------------------------

_ACCENTS = {"à": "a", "â": "a", "ä": "a", "é": "e", "è": "e", "ê": "e", "ë": "e",
            "î": "i", "ï": "i", "ô": "o", "ö": "o", "ù": "u", "û": "u", "ü": "u",
            "ç": "c"}


def _norm(s):
    """Minuscule + sans accents (pour matcher 'a faire' == 'à faire')."""
    return "".join(_ACCENTS.get(c, c) for c in (s or "").lower())


def route(text, rules=None):
    """Score chaque bucket par nombre de mots-cles trouves (mot entier, insensible
    casse/accents). Le plus haut gagne ; egalite -> premier declare ; zero -> defaut.
    Renvoie un dict {note, section, as_task, bucket, score}."""
    rules = rules or DEFAULT_ROUTING
    norm = _norm(text)
    best, best_score = None, 0
    for b in rules.get("buckets", []):
        score = 0
        for kw in b.get("keywords", []):
            k = _norm(kw)
            if k and re.search(r"(?<!\w)" + re.escape(k) + r"(?!\w)", norm):
                score += 1
        if score > best_score:
            best_score, best = score, b
    chosen = best if best else rules.get("default", DEFAULT_ROUTING["default"])
    return {
        "note": chosen.get("note", DAILY),
        "section": chosen.get("section"),
        "as_task": bool(chosen.get("as_task")),
        "bucket": (best or {}).get("name", "default"),
        "score": best_score,
    }


# --- Note du jour (s'adapte a la config du coffre) ---------------------------

_SAFE_FMT = re.compile(r"^[YMDHm\-_./: ]+$")


def _fmt_date(fmt, now):
    """Convertit un format Moment.js usuel (YYYY MM DD HH mm) en date. Replis surs
    sur %Y-%m-%d si le format est exotique (noms de mois, jours, etc.)."""
    fmt = fmt or "YYYY-MM-DD"
    if not _SAFE_FMT.match(fmt):
        return now.strftime("%Y-%m-%d")
    out = fmt
    for a, b in (("YYYY", "%Y"), ("MM", "%m"), ("DD", "%d"),
                 ("HH", "%H"), ("mm", "%M")):
        out = out.replace(a, b)
    if re.search(r"[A-Za-z]", re.sub(r"%.", "", out)):
        return now.strftime("%Y-%m-%d")
    try:
        return now.strftime(out)
    except Exception:
        return now.strftime("%Y-%m-%d")


def _daily_path(vault, now):
    """Chemin de la note du jour, lu depuis <coffre>/.obsidian/daily-notes.json
    (dossier + format), pour ecrire la ou Obsidian l'attend. Replis surs."""
    folder, fmt = "", "YYYY-MM-DD"
    try:
        p = os.path.join(vault, ".obsidian", "daily-notes.json")
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f) or {}
        folder = d.get("folder") or ""
        fmt = d.get("format") or "YYYY-MM-DD"
    except Exception:
        pass
    name = _fmt_date(fmt, now) + ".md"
    return os.path.join(vault, folder, name) if folder else os.path.join(vault, name)


# --- Ecriture -----------------------------------------------------------------

def _append_under_section(path, section, line):
    """Insere `line` juste sous l'en-tete `section` (cree note/section au besoin).
    Idempotent vis-a-vis de la structure ; n'ecrase jamais le contenu existant."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    if not os.path.exists(path):
        title = os.path.splitext(os.path.basename(path))[0]
        parts = ["# " + title, ""]
        parts += ([section, line, ""] if section else [line, ""])
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(parts))
        return
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    if section and section.strip() in [ln.strip() for ln in content.splitlines()]:
        out, done = [], False
        for ln in content.splitlines():
            out.append(ln)
            if not done and ln.strip() == section.strip():
                out.append(line)
                done = True
        new = "\n".join(out) + "\n"
    elif section:
        new = content.rstrip() + "\n\n" + section + "\n" + line + "\n"
    else:
        new = content.rstrip() + "\n" + line + "\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(new)


def capture(text, vault, now_ts=None, routing=None):
    """Range une pensee dictee dans le coffre. 100% local, defensif, ne leve jamais.
    Renvoie {ok, path, bucket} ou {ok: False, error}."""
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "vide"}
    if not vault or not os.path.isdir(vault):
        return {"ok": False, "error": "coffre introuvable"}
    try:
        now = _dt.datetime.fromtimestamp(now_ts) if now_ts else _dt.datetime.now()
    except Exception:
        now = _dt.datetime.now()

    rules = routing or load_routing(vault)
    r = route(text, rules)

    is_daily = (r["note"] == DAILY or not r["note"])
    if is_daily:
        note_path = _daily_path(vault, now)
        stamp = now.strftime("%H:%M")
    else:
        note_path = os.path.join(vault, r["note"])
        stamp = now.strftime("%Y-%m-%d %H:%M")

    prefix = "- [ ] " if r["as_task"] else "- "
    line = prefix + stamp + " " + text

    try:
        with _WRITE_LOCK:
            _append_under_section(note_path, r.get("section"), line)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "path": note_path, "bucket": r["bucket"]}
