#!/usr/bin/env python3
"""
Vlocal — Traitement de texte DÉTERMINISTE (glossaire + mise en forme par règles).

v15 — VIRAGE PRODUIT : plus AUCUNE IA générative. Le SLM (Ministral/Gemma) et
Ollama ont été entièrement supprimés. Ce module ne fait plus que du traitement
déterministe, instantané, sans réseau :

  - apply_glossary() : corrections noms propres / acronymes (find & replace),
    appliqué sur la sortie Whisper (la « passe 2 » du glossaire). La « passe 1 »
    (biais initial_prompt) vit dans engine.py.
  - format_by_rules() : ponctuation, paragraphage par pauses, suppression des
    tics, capitalisation — par règles pures (réunion avec segments + fallback
    texte continu sans segments).

Aucune dépendance externe. Aucune donnée ne quitte la machine.
"""

import re
import unicodedata

# --------------------------------------------------------------------------- #
# Glossaire de corrections (passe 2, sur la sortie Whisper)
# --------------------------------------------------------------------------- #
def _entry_pattern(incorrect: str):
    """Regex de l'entrée glossaire, frontières de mot intelligentes pour éviter
    qu'« Apify » ne matche dans « ApifyAtor »."""
    esc = re.escape(unicodedata.normalize("NFC", incorrect))
    left = r"\b" if incorrect[:1].isalnum() else ""
    right = r"\b" if incorrect[-1:].isalnum() else ""
    return re.compile(left + esc + right, re.IGNORECASE)


# v20 — Cache des patterns COMPILÉS. Avant : re.compile de chaque entrée à
# CHAQUE appel (chaque dictée + chaque bloc locuteur d'une réunion diarisée :
# 200 blocs x 20 entrées = 4000 compilations). Clé = tuple des paires ->
# invalidation automatique dès que glossaire/snippets changent. La sémantique
# d'application (passes séquentielles, ordre préservé, chaque sub voit le
# résultat du précédent) est STRICTEMENT inchangée : seule la compilation est
# mémoïsée -> sortie byte-identique.
_PATTERNS_CACHE = {"key": None, "compiled": None}


def _compiled_entries(entries):
    key = tuple((inc, cor) for inc, cor in entries)
    if _PATTERNS_CACHE["key"] == key:
        return _PATTERNS_CACHE["compiled"]
    compiled = []
    for inc, cor in key:
        if not inc:
            continue
        try:
            pat = _entry_pattern(inc)
        except Exception:
            continue
        cor_n = unicodedata.normalize("NFC", cor) if cor else ""
        compiled.append((pat, cor_n))
    _PATTERNS_CACHE["key"] = key
    _PATTERNS_CACHE["compiled"] = compiled
    return compiled


def apply_glossary(text: str, entries) -> str:
    """Find-and-replace insensible à la casse selon les entrées du glossaire.

    entries : itérable de (incorrect, correct). Ordre préservé.
    Si entries vide/None : texte inchangé.

    Robustesse : texte et entrées sont normalisés en Unicode NFC pour que les
    noms accentués (médecine/droit : « Néolife ») matchent quelle que soit leur
    forme de composition (NFC vs NFD selon la source de saisie). Le remplacement
    est traité comme LITTÉRAL (pas comme un motif regex : un « \\1 » dans la
    correction ne serait pas interprété comme une référence arrière).
    """
    if not text or not entries:
        return text or ""
    text = unicodedata.normalize("NFC", text)
    for pat, cor_n in _compiled_entries(entries):
        text = pat.sub(lambda _m, c=cor_n: c, text)
    return text


# --------------------------------------------------------------------------- #
# Mise en forme par règles : réunion (avec segments) + fallback texte continu
# (sans segments) — zéro IA, fidèle par construction
# --------------------------------------------------------------------------- #
# Tics oraux supprimés (uniquement les tics manifestes, jamais de reformulation).
_TICS_RE = re.compile(
    r"\b(euh+|heu+|hum+|hein|ben|bah|beh|"
    r"tu vois|vous voyez|du coup|en fait|genre)\b[\s,]*",
    re.IGNORECASE)
# Tics d'amorce de phrase (en début de phrase uniquement).
_LEAD_TICS_RE = re.compile(
    r"(^|[.!?]\s+)(alors|voil[àa]|enfin|bon|donc|et donc|ben)\b[\s,]*",
    re.IGNORECASE)
# Pause (silence) au-delà de laquelle on crée un nouveau paragraphe (RÉUNION).
PARAGRAPH_GAP_S = 2.0


def _strip_tics(text: str) -> str:
    if not text:
        return ""
    t = _TICS_RE.sub(" ", text)
    t = _LEAD_TICS_RE.sub(lambda m: m.group(1), t)
    return t


def _fix_spacing(text: str) -> str:
    """Espaces propres autour de la ponctuation FR (sans toucher au contenu)."""
    t = re.sub(r"[ \t]+", " ", text)
    t = re.sub(r" +([,.;:!?…»%)\]])", r"\1", t)   # pas d'espace AVANT
    t = re.sub(r"([(\[«]) +", r"\1", t)            # pas d'espace APRÈS ouvrants
    # une espace APRÈS , ; : ! ? si collé à une lettre/chiffre (pas pour les .
    # des nombres/acronymes : on traite . à part en exigeant une majuscule après).
    t = re.sub(r"([,;:!?…])(?=[A-Za-zÀ-ÿ0-9])", r"\1 ", t)
    t = re.sub(r"(\.)(?=[A-ZÀ-Ÿ])", r"\1 ", t)     # point collé devant majuscule
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n[ \t]+", "\n", t)
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def _capitalize_sentences(text: str) -> str:
    """Majuscule en début de phrase (après . ! ? et en tout début)."""
    if not text:
        return text
    text = re.sub(
        r"([.!?]\s+)([a-zà-ÿ])",
        lambda m: m.group(1) + m.group(2).upper(),
        text)
    # début de chaque paragraphe
    text = re.sub(
        r"(^|\n)([a-zà-ÿ])",
        lambda m: m.group(1) + m.group(2).upper(),
        text)
    return text


def format_by_rules(text: str, segments=None) -> str:
    """Mise en forme déterministe d'une transcription Whisper.

    - supprime les tics oraux,
    - paragraphe par PAUSES (si `segments` avec start/end fournis : nouveau
      paragraphe dès qu'un silence > PARAGRAPH_GAP_S sépare deux segments),
    - corrige les espaces autour de la ponctuation,
    - capitalise les débuts de phrase.

    Ne reformule RIEN, n'invente RIEN : c'est fidèle par construction.
    `segments` : liste de dicts {text, start, end} (optionnel).
    """
    if segments:
        paragraphs, current, last_end = [], [], None
        for seg in segments:
            try:
                start = float(seg.get("start", 0.0))
                end = float(seg.get("end", start))
            except Exception:
                start = end = 0.0
            stext = _strip_tics((seg.get("text") or "").strip())
            if last_end is not None and (start - last_end) > PARAGRAPH_GAP_S \
                    and current:
                paragraphs.append(" ".join(current).strip())
                current = []
            if stext:
                current.append(stext)
            last_end = end
        if current:
            paragraphs.append(" ".join(current).strip())
        out = "\n\n".join(p for p in paragraphs if p.strip())
    else:
        out = _strip_tics(text or "")

    out = _fix_spacing(out)
    out = _capitalize_sentences(out)
    out = _fix_spacing(out)
    out = out.strip()
    # Point final s'il manque (uniquement sur un texte d'un seul paragraphe ;
    # sur du multi-paragraphes chaque para garde sa ponctuation d'origine).
    if out and "\n" not in out and out[-1] not in ".!?…»":
        out += "."
    return out


# --------------------------------------------------------------------------- #
# v1.0.14 — Commandes vocales de mise en forme (saut de ligne + ponctuation).
# DÉTERMINISTE, isolé, sans IA. Appliqué en DICTÉE/NOTE après le glossaire et
# avant l'insertion. Principe de précaution : dans le doute, on ne fait rien ;
# on ne supprime jamais de contenu utilisateur.
# --------------------------------------------------------------------------- #
# Un saut de ligne se déclenche dans DEUX cas sûrs :
#  (a) la commande est PRÉCÉDÉE d'un marqueur de fin/clôture (« ..., à la ligne, ... »)
#  (b) la commande est SUIVIE d'une ponctuation collée — point, ? ! OU VIRGULE.
#      C'est le cas RÉEL en dictée : pour marquer la pause autour de la commande,
#      Whisper écrit « ...tu vas bien à la ligne. Est-ce... » ou « ça va, à la
#      ligne, j'espère... ». On consomme cette ponctuation (rendu de la pause) et
#      on absorbe une virgule de pause juste AVANT pour un saut net.
# Les contre-exemples (« à la ligne de bus », « à la ligne maintenant », « à la
# ligne des toits ») n'ont NI marqueur avant NI ponctuation COLLÉE après (un mot
# s'intercale) -> jamais coupés.
_VC_END_MARKERS = r"([.!?,;]|\bvoilà\b|\bdonc\b|\bok\b|\bbien\b|\bparfait\b|\bmerci\b|\bstop\b|\bbref\b)"
_VC_NEWLINE_CMDS = r"(à la ligne|retour à la ligne|nouvelle ligne|saut de ligne)"
_VC_PARAGRAPH_CMDS = r"(nouveau paragraphe|paragraphe suivant)"
# (a) marqueur AVANT la commande
_VC_PARAGRAPH_RE = re.compile(r"(?i)(%s)\s+%s\s+" % (_VC_END_MARKERS, _VC_PARAGRAPH_CMDS))
_VC_NEWLINE_RE = re.compile(r"(?i)(%s)\s+%s\s+" % (_VC_END_MARKERS, _VC_NEWLINE_CMDS))
# (b) ponctuation collée APRÈS (et virgule de pause éventuelle juste avant)
_VC_PARAGRAPH_AFTER_RE = re.compile(r"(?i)[ \t]*,?[ \t]*%s[ \t]*[,.;!?]+" % _VC_PARAGRAPH_CMDS)
_VC_NEWLINE_AFTER_RE = re.compile(r"(?i)[ \t]*,?[ \t]*%s[ \t]*[,.;!?]+" % _VC_NEWLINE_CMDS)
# Ponctuation vocale sans ambiguïté (toujours remplacée). Exclus car trop
# ambigus -> risque de perte de contenu : « point » seul (« point de vue »,
# « au point ») et « deux points » (« on a deux points à voir » deviendrait
# « on a : à voir »). Principe de précaution : dans le doute, on n'y touche pas.
_VC_PUNCT = [
    (re.compile(r"(?i)\bvirgule\b"), ", "),
    (re.compile(r"(?i)\bpoint virgule\b"), "; "),
    (re.compile(r"(?i)\bpoint d['’]interrogation\b"), "? "),
    (re.compile(r"(?i)\bpoint d['’]exclamation\b"), "! "),
    (re.compile(r"(?i)\bouvrir les guillemets\b"), "« "),
    (re.compile(r"(?i)\bfermer les guillemets\b"), " »"),
    (re.compile(r"(?i)\bouvrir la parenth[èe]se\b"), "("),
    (re.compile(r"(?i)\bfermer la parenth[èe]se\b"), ")"),
    (re.compile(r"(?i)\btiret\b"), "-"),
]


def apply_vocal_commands(text: str) -> str:
    """Remplace les commandes vocales de mise en forme dites pendant la dictée.

    Sauts de ligne (« à la ligne », « nouveau paragraphe »…) : UNIQUEMENT s'ils
    suivent un marqueur de fin/clôture (ponctuation ou « voilà/donc/ok… ») —
    jamais au milieu d'une phrase. Ponctuation vocale (« virgule », « point
    d'interrogation »…) : déterministe, sans ambiguïté.

    Principe de précaution : dans le doute, rien. Ne supprime jamais de contenu.
    En cas d'erreur inattendue : retourne le texte d'origine intact.
    """
    if not text or not text.strip():
        return ""
    original = text
    try:
        # (b) commande SUIVIE d'une ponctuation collée (point/?/!/virgule) -> saut.
        # La ponctuation est consommée (c'était le rendu de la pause). C'est le cas
        # réel en dictée naturelle ; on le traite AVANT la règle (a).
        text = _VC_PARAGRAPH_AFTER_RE.sub("\n\n", text)
        text = _VC_NEWLINE_AFTER_RE.sub("\n", text)
        # (a) commande PRÉCÉDÉE d'un marqueur -> saut (on garde le marqueur).
        text = _VC_PARAGRAPH_RE.sub(r"\1\n\n", text)
        text = _VC_NEWLINE_RE.sub(r"\1\n", text)
        for pat, rep in _VC_PUNCT:
            text = pat.sub(rep, text)
        # Nettoyage des espaces résiduels (sans toucher au contenu).
        text = re.sub(r" +\n", "\n", text)
        text = re.sub(r"\n +", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r"  +", " ", text)
        text = re.sub(r" +,", ",", text)   # FR : jamais d'espace avant la virgule
        return text.strip()
    except Exception:
        return original
