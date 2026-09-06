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


# --- Coffre « Vlocal, ma voix » (v1.1.0) ---------------------------------------
#
# Un coffre dédié, créé par Vlocal, où TOUT ce qui est dit dans l'app est rangé
# de façon structurée : dictées (note du jour), réunions (une note par réunion,
# avec les locuteurs) et rappels. Le but est double : relire dans Obsidian, et
# donner à un assistant (Claude Code ouvert sur le coffre) le contexte réel de
# ce sur quoi la personne travaille. Un CLAUDE.md à la racine explique la
# structure. Tout reste local ; le partage est décidé par l'utilisateur.

VOICE_VAULT_NAME = "Vlocal, ma voix"
DICTATIONS_DIR = "Dictées"
MEETINGS_DIR = "Réunions"
REMINDERS_DIR = "Rappels"

_VOICE_ROUTING = {
    "version": 1,
    "default": {"note": DAILY, "section": "## Dictées"},
    "buckets": [
        {
            "name": "Taches",
            "note": DAILY,
            "section": "## À faire",
            "as_task": True,
            "keywords": [
                "a faire", "rappelle-moi", "rappelle moi", "rappel", "deadline",
                "todo", "penser a", "ne pas oublier", "il faut que", "noter de",
            ],
        },
    ],
}

_VOICE_README = """# Vlocal, ma voix

Ce coffre est écrit par Vlocal, l'application de dictée et de transcription
100 % locale. Rien ici n'a transité par un serveur : chaque note vient d'une
dictée, d'une réunion ou d'un rappel enregistré sur ce Mac.

## Structure

- `Dictées/AAAA-MM-JJ.md` : une note par jour. Section « Dictées » (horodatées)
  et section « À faire » (les phrases qui ressemblent à une tâche, en cases à
  cocher).
- `Réunions/AAAA-MM-JJ HHMM Titre.md` : une note par réunion, avec la date, la
  durée, les participants reconnus et la transcription par locuteur.
- `Rappels/Rappels.md` : les rappels créés à la voix, avec leur échéance.
- `.vlocal/routing.json` : règles de rangement des dictées (modifiables).

Les fichiers sont du Markdown ordinaire : ils se lisent dans Obsidian, dans un
éditeur, ou par un assistant à qui vous donnez accès au dossier.
"""

_VOICE_CLAUDE_MD = """# Contexte : coffre « Vlocal, ma voix »

Ce dossier contient tout ce que son propriétaire a dicté ou enregistré avec
Vlocal (dictée vocale et transcription de réunions, traitement 100 % local).
Il sert de mémoire de travail : ce que la personne fait, à qui elle parle, ce
qu'elle a décidé.

Comment l'utiliser :
- `Dictées/` : notes quotidiennes, les plus récentes d'abord pour comprendre
  le contexte du moment. Les lignes « - [ ] » sont des tâches ouvertes.
- `Réunions/` : transcriptions par locuteur. Les noms viennent de la
  reconnaissance des voix de Vlocal ; « Voix 1 », « Voix 2 » sont des
  locuteurs non nommés.
- `Rappels/` : rappels datés.

Ces textes sont des transcriptions automatiques : noms propres et homophones
peuvent être approximatifs. Ne pas réécrire ces fichiers, Vlocal les complète
au fil de l'eau.
"""


def default_voice_vault_path():
    return os.path.join(os.path.expanduser("~/Documents"), VOICE_VAULT_NAME)


def _register_in_obsidian(path):
    """Ajoute le coffre à la liste des coffres connus d'Obsidian (obsidian.json),
    pour qu'il apparaisse dans le sélecteur. Best-effort : si le fichier de
    config n'existe pas (Obsidian jamais lancé), on ne crée rien."""
    try:
        if not os.path.exists(_OBSIDIAN_CONFIG):
            return False
        with open(_OBSIDIAN_CONFIG, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        vaults = data.setdefault("vaults", {})
        for v in vaults.values():
            if isinstance(v, dict) and os.path.realpath(v.get("path") or "") == os.path.realpath(path):
                return True
        import secrets
        vaults[secrets.token_hex(8)] = {"path": path,
                                        "ts": int(_dt.datetime.now().timestamp() * 1000)}
        tmp = _OBSIDIAN_CONFIG + ".vlocal.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, _OBSIDIAN_CONFIG)
        return True
    except Exception:
        return False


def create_voice_vault(base=None):
    """Crée (ou complète) le coffre « Vlocal, ma voix ». Idempotent : ne touche
    jamais une note existante. Renvoie {ok, path, created}."""
    path = base or default_voice_vault_path()
    try:
        created = not os.path.isdir(path)
        for sub in (".obsidian", ".vlocal", DICTATIONS_DIR, MEETINGS_DIR, REMINDERS_DIR):
            os.makedirs(os.path.join(path, sub), exist_ok=True)
        daily_cfg = os.path.join(path, ".obsidian", "daily-notes.json")
        if not os.path.exists(daily_cfg):
            with open(daily_cfg, "w", encoding="utf-8") as f:
                json.dump({"folder": DICTATIONS_DIR, "format": "YYYY-MM-DD"}, f)
        app_cfg = os.path.join(path, ".obsidian", "app.json")
        if not os.path.exists(app_cfg):
            with open(app_cfg, "w", encoding="utf-8") as f:
                json.dump({}, f)
        for name, body in (("README.md", _VOICE_README), ("CLAUDE.md", _VOICE_CLAUDE_MD)):
            p = os.path.join(path, name)
            if not os.path.exists(p):
                with open(p, "w", encoding="utf-8") as f:
                    f.write(body)
        seed_routing(path, _VOICE_ROUTING)
        _register_in_obsidian(path)
        return {"ok": True, "path": path, "created": created}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _safe_title(s, limit=60):
    s = re.sub(r"[\\/:*?\"<>|\n\r\t]+", " ", (s or "")).strip()
    s = re.sub(r"\s+", " ", s)
    return (s[:limit].rstrip() or "Réunion")


def capture_meeting(meeting, vault, blocks=None, names=None):
    """Écrit (ou réécrit) la note d'une réunion : Réunions/AAAA-MM-JJ HHMM Titre.md.
    `meeting` = ligne SQLite (dict) ; `blocks` = [{speaker,start,end,text}] ;
    `names` = {speaker_id: nom}. Une note par réunion (fichier stable via l'id),
    remplacée à chaque ré-analyse ou renommage. Ne lève jamais."""
    try:
        if not vault or not os.path.isdir(vault) or not meeting:
            return {"ok": False, "error": "coffre introuvable"}
        mid = int(meeting.get("id") or 0)
        try:
            when = _dt.datetime.fromtimestamp(float(meeting.get("created_at") or 0))
        except Exception:
            when = _dt.datetime.now()
        title = _safe_title(meeting.get("titre") or "Réunion")
        folder = os.path.join(vault, MEETINGS_DIR)
        os.makedirs(folder, exist_ok=True)
        stem = when.strftime("%Y-%m-%d %H%M") + " " + title
        path = os.path.join(folder, stem + ".md")
        # fichier stable par réunion : un marqueur d'id retrouve l'ancien nom
        marker = f"vlocal_meeting_id: {mid}"
        for fn in os.listdir(folder):
            fp = os.path.join(folder, fn)
            if fn.endswith(".md") and fp != path:
                try:
                    with open(fp, "r", encoding="utf-8") as f:
                        head = f.read(400)
                    if marker in head:
                        path = fp
                        break
                except Exception:
                    pass
        names = names or {}
        dur = float(meeting.get("duree_audio_s") or 0)
        speakers = []
        if blocks:
            for b in blocks:
                sid = b.get("speaker")
                if sid not in speakers:
                    speakers.append(sid)
        parts = ["---", marker, f"date: {when.strftime('%Y-%m-%d %H:%M')}",
                 f"durée_min: {int(round(dur / 60.0))}",
                 "participants: [" + ", ".join(
                     f"\"{names.get(s) or ('Voix ' + str(i + 1))}\"" for i, s in enumerate(speakers)) + "]",
                 "---", "", "# " + title, ""]
        if blocks:
            for b in blocks:
                who = names.get(b.get("speaker")) or ("Voix %d" % (speakers.index(b.get("speaker")) + 1))
                t = int(float(b.get("start") or 0))
                parts.append(f"**{who}** ({t // 60:02d}:{t % 60:02d})")
                parts.append((b.get("text") or "").strip())
                parts.append("")
        else:
            parts.append((meeting.get("transcription_structuree")
                          or meeting.get("transcription_brute") or "").strip())
            parts.append("")
        with _WRITE_LOCK:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(parts))
        return {"ok": True, "path": path}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def capture_reminder(titre, echeance, vault, now_ts=None):
    """Ajoute un rappel à Rappels/Rappels.md (case à cocher, échéance). Ne lève jamais."""
    try:
        if not vault or not os.path.isdir(vault):
            return {"ok": False, "error": "coffre introuvable"}
        now = _dt.datetime.fromtimestamp(now_ts) if now_ts else _dt.datetime.now()
        path = os.path.join(vault, REMINDERS_DIR, "Rappels.md")
        line = f"- [ ] {(titre or '').strip()} (échéance : {(echeance or '').strip() or 'non précisée'}, dicté le {now.strftime('%Y-%m-%d %H:%M')})"
        with _WRITE_LOCK:
            _append_under_section(path, "## Rappels", line)
        return {"ok": True, "path": path}
    except Exception as e:
        return {"ok": False, "error": str(e)}
