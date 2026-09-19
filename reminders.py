#!/usr/bin/env python3
"""
Vlocal — Parseur de rappels LOCAL, déterministe, instantané (FR).

Aucune IA, aucune dépendance externe, aucune donnée ne sort de la machine.
Couvre un large éventail de formulations françaises :

  Déclencheurs : rappelle-moi, rappelle moi, rappel, n'oublie pas, souviens-toi,
                 pense à, il faut que je, faut que je, note de, à faire…
  Heures       : à 18h, à 18h30, à 18 heures, à 18 heures 30, à 8h du soir,
                 à 8h du matin, à midi, à minuit, à 6h et demie, à 9h et quart,
                 7h pile, vers 18h, avant 18h (échéance), un peu avant 18h.
  Relatif      : dans 2 heures, dans une heure, dans 30 minutes, dans 10 min,
                 dans un quart d'heure, dans une demi-heure, dans 3 jours,
                 d'ici 2 heures.
  Jours        : aujourd'hui, demain, après-demain, ce soir, ce matin, ce midi,
                 cet après-midi, demain matin/soir, lundi, lundi prochain,
                 ce week-end.
  Dates        : le 15, le 15 juin, le 20 juin à 14h.

API : parse_reminder(raw_text, now_ts) -> dict | None
"""

import datetime as _dt
import re
import unicodedata

DEFAULT_TIMES = {
    "matin": (9, 0), "midi": (12, 0), "apres-midi": (14, 0),
    "soir": (19, 0), "nuit": (22, 0),
}
DEFAULT_DAY_HOUR = (9, 0)   # jour sans heure -> 9h

WEEKDAY_NAMES = ("lundi", "mardi", "mercredi", "jeudi",
                 "vendredi", "samedi", "dimanche")
WEEKDAYS = {n: i for i, n in enumerate(WEEKDAY_NAMES)}
MONTHS = {
    "janvier": 1, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6,
    "juillet": 7, "aout": 8, "septembre": 9, "octobre": 10, "novembre": 11,
    "decembre": 12,
}
# Mots-nombres FR couverts : 1..21 (formes avec traits d'union uniquement) et 30.
WORD_NUM = {
    "un": 1, "une": 1, "deux": 2, "trois": 3, "quatre": 4, "cinq": 5,
    "six": 6, "sept": 7, "huit": 8, "neuf": 9, "dix": 10, "onze": 11,
    "douze": 12, "treize": 13, "quatorze": 14, "quinze": 15, "seize": 16,
    "dix-sept": 17, "dix-huit": 18, "dix-neuf": 19, "vingt": 20,
    "vingt-et-un": 21, "trente": 30,
}

_LEAD_PREFIXES = [
    # "verbe + un rappel" (fais-moi un rappel, mets un rappel, effectue…)
    r"peux[- ]?tu (?:me )?(?:faire|mettre|ajouter|programmer|planifier|cr[ée]er) (?:un|le) rappel",
    r"fais[- ]?moi (?:un|le) rappel", r"fais[- ]?nous (?:un|le) rappel",
    r"(?:mets|met|ajoute|cr[ée]e|cree|programme|planifie|effectue|fait|fais)[- ]?(?:moi )?(?:un|le) rappel",
    r"un rappel pour", r"rappel pour", r"rappel de",
    # "rappelle-moi" et co
    r"rappelle?[- ]?moi", r"rappel[- ]?moi", r"rappelle?[- ]?nous",
    r"tu (?:peux )?me rappelle?r?s?", r"peux[- ]?tu me rappeler",
    r"qu'?il faut que je", r"qu'?il faut", r"il faut que je pense a",
    r"il faut que je", r"il faut", r"faut que je", r"faut que j'?",
    r"je dois (?:penser a )?", r"je ne dois pas oublier de",
    r"pense[r]? a", r"penser a", r"penses[- ]?y a",
    r"n'?oublie pas (?:de |que )?", r"n'?oublie pas", r"note de", r"a faire",
    r"rappel",
]
_LEAD_CONNECTORS = [r"^de\s+", r"^d'", r"^que\s+", r"^qu'", r"^a\s+", r"^pour\s+"]


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if unicodedata.category(c) != "Mn")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", _strip_accents((s or "").lower())).strip()


def _cap(s: str) -> str:
    s = (s or "").strip()
    return s[0].upper() + s[1:] if s else s


def _num(tok: str):
    """Token -> entier (chiffres ou mot-nombre), ou None."""
    tok = tok.strip()
    if tok.isdigit():
        return int(tok)
    return WORD_NUM.get(tok)


def _next_weekday(base: _dt.datetime, idx: int, force_next=False) -> _dt.datetime:
    days = (idx - base.weekday()) % 7
    if days == 0 and force_next:
        days = 7
    return base + _dt.timedelta(days=days)


# --------------------------------------------------------------------------- #
# Extraction de l'heure
# --------------------------------------------------------------------------- #
def _extract_time(norm: str):
    """Cherche une heure explicite -> {h, m, label, span} ou None."""
    # minuit / midi. v20 — BUG corrigé : « \bmidi\b » matchait DANS
    # « apres-midi » (le tiret/l'espace est une frontière de mot) -> « cet
    # après-midi » devenait 12:00 au lieu du moment 14:00, et le titre était
    # amputé. Lookbehinds fixes sur les deux formes normalisées. On gère aussi
    # « midi/minuit et demi(e) / et quart / moins le quart ».
    def _half_quarter(base_h, m, label):
        info = {"h": base_h, "m": 0, "label": label, "span": list(m.span())}
        tail = norm[m.end():m.end() + 18]
        mm = re.match(r"\s*(et\s+demie?|et\s+quart|moins\s+(?:le\s+)?quart)", tail)
        if mm:
            frag = mm.group(1)
            if "demi" in frag:
                info["m"] = 30
            elif "moins" in frag:
                info["h"] = (base_h - 1) % 24
                info["m"] = 45
            else:
                info["m"] = 15
            info["span"][1] = m.end() + mm.end()
            info["label"] = norm[m.start():info["span"][1]]
        return info

    m = re.search(r"\bminuit\b", norm)
    if m:
        return _half_quarter(0, m, "minuit")
    m = re.search(r"(?<!apres-)(?<!apres )\bmidi\b", norm)
    if m:
        return _half_quarter(12, m, "midi")
    # "Xh", "XhY", "X h Y", "X heures", "X heures Y", "X:Y"
    m = re.search(r"\b(\d{1,2})\s*(?:h(?:eures?)?|:)\s*(\d{1,2})?\b", norm)
    if m:
        h = int(m.group(1))
        mn = int(m.group(2)) if m.group(2) else 0
        if 0 <= h <= 23 and 0 <= mn <= 59:
            info = {"h": h, "m": mn, "label": m.group(0).strip(), "span": list(m.span())}
            # "et demie / et quart / moins le quart" juste après
            tail = norm[m.end():m.end() + 18]
            mm = re.match(r"\s*(et\s+demie?|et\s+quart|moins\s+(?:le\s+)?quart|et\s+\d{1,2})", tail)
            if mm and not m.group(2):
                frag = mm.group(1)
                if "demi" in frag:
                    info["m"] = 30
                elif "quart" in frag and "moins" in frag:
                    info["h"] = (h - 1) % 24
                    info["m"] = 45
                elif "quart" in frag:
                    info["m"] = 15
                else:  # "et 30"
                    d = re.search(r"\d{1,2}", frag)
                    if d:
                        mn2 = int(d.group(0))
                        if 0 <= mn2 <= 59:        # cohérent avec la validation l.117
                            info["m"] = mn2       # "et 60/99" invalide -> ignoré
                info["span"][1] = m.end() + mm.end()
            return info
    return None


# --------------------------------------------------------------------------- #
# Cœur temporel
# --------------------------------------------------------------------------- #
def parse_datetime(raw_text: str, now: _dt.datetime):
    norm = _norm(raw_text)
    spans, lead_minutes, deadline = [], 0, False

    # Échéance "avant"
    if re.search(r"\b(un peu avant|juste avant)\b", norm):
        lead_minutes, deadline = 10, True
    elif re.search(r"\bavant\b", norm):
        lead_minutes, deadline = 15, True

    # 1) Relatif "dans / d'ici X (unité)" + cas spéciaux quart/demi-heure
    m = re.search(r"\b(?:dans|d['’]?ici)\s+un\s+quart\s+d['’]?heure\b", norm)
    if m:
        return now + _dt.timedelta(minutes=15), "dans un quart d'heure", 0.95, lead_minutes, [m.span()]
    m = re.search(r"\b(?:dans|d['’]?ici)\s+(?:une?\s+)?demi[e]?[- ]?heure\b", norm)
    if m:
        return now + _dt.timedelta(minutes=30), "dans une demi-heure", 0.95, lead_minutes, [m.span()]
    m = re.search(r"\b(?:dans|d['’]?ici)\s+trois\s+quarts?\s+d['’]?heure\b", norm)
    if m:
        return now + _dt.timedelta(minutes=45), "dans trois quarts d'heure", 0.95, lead_minutes, [m.span()]
    m = re.search(
        r"\b(?:dans|d['’]?ici)\s+([\w-]+)\s*"
        r"(min(?:ute)?s?|h(?:eures?)?|jours?|semaines?)\b", norm)
    if m:
        n = _num(m.group(1))
        unit = m.group(2)
        if n is not None:
            if unit.startswith("min"):
                target = now + _dt.timedelta(minutes=n)
                nat = f"dans {n} minute" + ("s" if n > 1 else "")
            elif unit.startswith("h"):
                target = now + _dt.timedelta(hours=n)
                nat = f"dans {n} heure" + ("s" if n > 1 else "")
            elif unit.startswith("semaine"):
                target = (now + _dt.timedelta(weeks=n)).replace(
                    hour=DEFAULT_DAY_HOUR[0], minute=DEFAULT_DAY_HOUR[1],
                    second=0, microsecond=0)
                nat = f"dans {n} semaine" + ("s" if n > 1 else "")
            else:  # jours -> ce jour-là à 9h (rappel "de la journée")
                target = (now + _dt.timedelta(days=n)).replace(
                    hour=DEFAULT_DAY_HOUR[0], minute=DEFAULT_DAY_HOUR[1],
                    second=0, microsecond=0)
                nat = f"dans {n} jour" + ("s" if n > 1 else "")
            spans.append(m.span())
            return target, nat, 0.95, lead_minutes, spans

    # 2) Jour de référence
    day_base, day_label, day_rollable = None, "", False
    def _set_day(dtv, label, span, rollable=False):
        nonlocal day_base, day_label, day_rollable
        day_base = dtv.replace(hour=0, minute=0, second=0, microsecond=0)
        day_label = label
        day_rollable = rollable     # True pour jour de semaine / week-end
        spans.append(span)

    mm = re.search(r"\bce week[- ]?end\b", norm)
    if re.search(r"\bapres[- ]?demain\b", norm):
        _set_day(now + _dt.timedelta(days=2), "après-demain",
                 re.search(r"\bapres[- ]?demain\b", norm).span())
    elif re.search(r"\bdemain\b", norm):
        _set_day(now + _dt.timedelta(days=1), "demain", re.search(r"\bdemain\b", norm).span())
    elif re.search(r"\baujourd['’]?hui\b", norm):
        _set_day(now, "aujourd'hui", re.search(r"\baujourd['’]?hui\b", norm).span())
    elif mm:
        # ce week-end -> prochain samedi (ou aujourd'hui si samedi)
        sat = _next_weekday(now, 5, force_next=False)
        _set_day(sat, "ce week-end", mm.span(), rollable=True)
    else:
        for wd, idx in WEEKDAYS.items():
            w = re.search(r"\b" + wd + r"(\s+prochain)?\b", norm)
            if w:
                force = bool(w.group(1))
                _set_day(_next_weekday(now, idx, force_next=force),
                         wd + (" prochain" if force else ""), w.span(), rollable=True)
                break

    # 2bis) Date explicite "le 15" / "le 15 juin"
    dm = re.search(r"\ble\s+(\d{1,2})(?:\s+(" + "|".join(MONTHS) + r"))?\b", norm)
    if day_base is None and dm:
        day = int(dm.group(1))
        month = MONTHS.get(dm.group(2)) if dm.group(2) else now.month
        year = now.year
        try:
            cand = now.replace(month=month, day=day, hour=0, minute=0, second=0, microsecond=0)
            if cand.date() < now.date():
                # date passée -> mois suivant (si pas de mois nommé) ou année suivante
                if dm.group(2):
                    cand = cand.replace(year=year + 1)
                else:
                    nm = month + 1
                    cand = cand.replace(year=year + (1 if nm > 12 else 0),
                                        month=(nm - 1) % 12 + 1)
            day_base = cand
            day_label = "le " + dm.group(0).split(None, 1)[1]
            spans.append(dm.span())
        except ValueError:
            pass

    # 3) Moment flou
    moment, moment_span = None, None
    for key, pat in [
        ("soir", r"\b(ce soir|le soir|du soir|soir)\b"),
        ("apres-midi", r"\b(cet? apres[- ]?midi|l'apres[- ]?midi|apres[- ]?midi)\b"),
        ("midi", r"\b(ce midi|le midi)\b"),
        ("matin", r"\b(ce matin|le matin|du matin|demain matin|matin)\b"),
        ("nuit", r"\b(cette nuit|la nuit)\b"),
    ]:
        w = re.search(pat, norm)
        if w:
            moment, moment_span = key, w.span()
            break

    # 4) Heure explicite
    ti = _extract_time(norm)

    if ti:
        h, mn = ti["h"], ti["m"]
        if moment in ("soir", "nuit") and 1 <= h <= 11:
            h += 12
        elif moment == "apres-midi" and 1 <= h <= 6:
            h += 12
        base = day_base if day_base is not None else now.replace(second=0, microsecond=0)
        target = base.replace(hour=h, minute=mn, second=0, microsecond=0)
        prep = "avant" if deadline else "à"
        if day_base is None and target <= now:
            target += _dt.timedelta(days=1)
            nat = f"demain {prep} {ti['label']}"
        elif day_rollable and target <= now:
            # "samedi à 10h" alors qu'on est samedi 16h -> samedi prochain
            target += _dt.timedelta(days=7)
            nat = ((day_label + " ") if day_label else "") + f"{prep} {ti['label']}"
        else:
            nat = ((day_label + " ") if day_label else "") + f"{prep} {ti['label']}"
        spans.append(tuple(ti["span"]) if isinstance(ti["span"], list) else ti["span"])
        if moment_span:
            spans.append(moment_span)
        return target, nat.strip(), 0.93, lead_minutes, spans

    if moment is not None:
        h, mn = DEFAULT_TIMES[moment]
        base = day_base if day_base is not None else now.replace(second=0, microsecond=0)
        target = base.replace(hour=h, minute=mn, second=0, microsecond=0)
        if day_base is None and target <= now:
            target += _dt.timedelta(days=1)
        lbl = {"matin": "matin", "midi": "midi", "apres-midi": "après-midi",
               "soir": "soir", "nuit": "nuit"}[moment]
        nat = ((day_label + " ") if day_label else "") + lbl
        if moment_span:
            spans.append(moment_span)
        return target, nat.strip(), 0.8, lead_minutes, spans

    if day_base is not None:
        target = day_base.replace(hour=DEFAULT_DAY_HOUR[0], minute=DEFAULT_DAY_HOUR[1])
        # La DATE est >= aujourd'hui par construction, mais le datetime à 09:00
        # peut être passé si la date est aujourd'hui et now > 9h (day_rollable
        # n'est pas consommé dans cette branche).
        return target, day_label, 0.72, lead_minutes, spans

    return None, "", 0.0, lead_minutes, spans


# --------------------------------------------------------------------------- #
# Extraction du titre
# --------------------------------------------------------------------------- #
def _extract_title(raw_text: str) -> str:
    txt = (raw_text or "").strip()
    # 1) amorces (en boucle)
    changed = True
    while changed:
        changed = False
        norm = _norm(txt)
        for pat in _LEAD_PREFIXES:
            m = re.match(r"^\s*" + pat + r"\b", norm)
            if m:
                txt = txt[m.end():].strip()
                changed = True
                break
    # 2) expressions temporelles (sur l'original, insensible casse/accents)
    months = "|".join(MONTHS)
    temporal = [
        r"\b(?:dans|d['’]ici)\s+[\w-]+\s*(?:min(?:ute)?s?|h(?:eures?)?|jours?|semaines?)\b",
        r"\b(?:dans|d['’]ici)\s+un\s+quart\s+d['’]heure\b",
        r"\b(?:dans|d['’]ici)\s+(?:une?\s+)?demi[e]?[- ]?heure\b",
        r"\b(?:dans|d['’]ici)\s+trois\s+quarts?\s+d['’]heure\b",
        # v20 — les LOCUTIONS LONGUES d'abord : « \bmidi\b » appliqué avant
        # « cet après-midi » amputait la locution (titre « ...cet après »).
        r"\bce week[- ]?end\b",
        r"\bapr[èe]s[- ]?demain\b", r"\bdemain\b", r"\baujourd['’]?hui\b",
        r"\bce soir\b", r"\bcet? apr[èe]s[- ]?midi\b", r"\bce matin\b",
        r"\bce midi\b", r"\bà midi\b",
        r"\b(?:le|du|de la|cette)\s+(?:soir|matin|apr[èe]s[- ]?midi|midi|nuit)\b",
        r"\b(matin|soir|apr[èe]s[- ]?midi|midi|nuit)\b",
        r"\bminuit\b(?:\s*(?:et\s+demie?|et\s+quart|moins\s+(?:le\s+)?quart))?",
        r"\bmidi\b(?:\s*(?:et\s+demie?|et\s+quart|moins\s+(?:le\s+)?quart))?",
        r"\b\d{1,2}\s*(?:h(?:eures?)?|:)\s*\d{0,2}\b(?:\s*(?:et\s+demie?|et\s+quart|moins\s+(?:le\s+)?quart|pile))?",
        r"\bet\s+demie?\b", r"\bet\s+quart\b",
        r"\ble\s+\d{1,2}(?:\s+(?:" + months + r"))?\b",
        r"\bun peu avant\b", r"\bjuste avant\b", r"\bavant\b", r"\bvers\b", r"\bpile\b",
        r"\bdu (?:soir|matin)\b",
    ]
    for wd in WEEKDAYS:
        temporal.append(r"\b" + wd + r"(?:\s+prochain)?\b")
    for pat in temporal:
        txt = re.sub(pat, " ", txt, flags=re.IGNORECASE)
    # 3) connecteurs en tête
    txt = re.sub(r"\s+", " ", txt).strip()
    changed = True
    while changed:
        changed = False
        low = _strip_accents(txt.lower())
        for pat in _LEAD_CONNECTORS:
            m = re.match(pat, low)
            if m:
                txt = txt[m.end():].strip()
                changed = True
                break
    # 3bis) prépositions/connecteurs orphelins en fin
    trailing = {"a", "à", "de", "d", "pour", "vers", "le", "la", "les", "du",
                "des", "et", "que", "qu", "avant", "ce", "cette"}
    changed = True
    while changed:
        changed = False
        txt = txt.strip(" ,.;:–-—'’\"")
        parts = txt.split()
        if parts and _strip_accents(parts[-1].lower().rstrip("'’")) in trailing:
            txt = " ".join(parts[:-1])
            changed = True
    txt = re.sub(r"\s+", " ", txt).strip(" ,.;:–-—'’\"").strip()
    return _cap(txt)


# Déclencheurs FORTS et non ambigus -> auto-détection sûre d'un rappel à
# l'intérieur d'une dictée (zéro faux positif sur de la dictée normale). On
# évite volontairement les tournures ambiguës (« il faut », « je dois ») qui
# apparaissent dans le langage courant. Texte normalisé (minuscule, sans accents).
_AUTO_REMINDER_RE = re.compile(
    r"\b(rappelle?[- ]?(?:moi|nous)|rappel[- ]?(?:moi|nous)|"
    r"(?:peux[- ]?tu|tu peux|tu pourrais) me rappeler|"
    r"n'?oublie pas (?:de |que )|n'?oublie pas\b|"
    r"pense[r]? (?:bien )?a |penses[- ]?y\b|"
    r"(?:fais|fait|mets|met|ajoute|cree?|creer|programme|planifie)[- ]?(?:moi |nous )?(?:un|le) rappel|"
    r"un rappel pour |rappel pour |note de )")


def is_reminder(raw_text: str) -> bool:
    """True si le texte dicté EST manifestement un rappel (déclencheur fort
    présent). Sert à l'auto-détection en mode Dictée -> création automatique
    d'un rappel, sinon dictée normale."""
    if not raw_text or not raw_text.strip():
        return False
    return bool(_AUTO_REMINDER_RE.search(_norm(raw_text)))


def parse_reminder(raw_text: str, now_ts: float):
    if not raw_text or not raw_text.strip():
        return None
    now = _dt.datetime.fromtimestamp(now_ts)
    target, nat, conf, lead, _spans = parse_datetime(raw_text, now)
    titre = _extract_title(raw_text)
    if not titre:
        titre = _cap(re.sub(r"\s+", " ", raw_text).strip())[:60]
        conf = min(conf, 0.5)
    datetime_iso = None
    if target is not None:
        if lead:
            target = target - _dt.timedelta(minutes=lead)
        datetime_iso = target.strftime("%Y-%m-%d %H:%M")
    return {
        "titre": titre[:80],
        "datetime_iso": datetime_iso,
        "echeance_naturelle": nat or "aucune",
        "confidence": round(conf, 2),
        "lead_minutes": lead,
    }


def humanize_when(datetime_iso: str, now_ts: float) -> str:
    if not datetime_iso:
        return ""
    try:
        target = _dt.datetime.fromisoformat(datetime_iso.replace("T", " "))
    except Exception:
        return ""
    now = _dt.datetime.fromtimestamp(now_ts)
    d0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    delta = (target.replace(hour=0, minute=0, second=0, microsecond=0) - d0).days
    hm = target.strftime("%H:%M")
    if delta == 0:
        return f"aujourd'hui à {hm}"
    if delta == 1:
        return f"demain à {hm}"
    if 2 <= delta <= 6:
        return f"{WEEKDAY_NAMES[target.weekday()]} à {hm}"
    return f"le {target.strftime('%d/%m')} à {hm}"
