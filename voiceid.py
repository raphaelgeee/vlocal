"""
WhoTalks v1.2 — Banque de VOIX CONNUES (reconnaissance inter-réunions).

Idée : chaque interlocuteur a une empreinte vocale stable — le CENTROÏDE de ses
empreintes CAM++ (vecteur 192-d L2-normalisé). Une fois qu'on a entendu une
personne assez longtemps ET qu'on lui a donné un nom, on stocke ce centroïde.
À la réunion suivante, on compare les centroïdes des voix détectées aux voix
connues : si ça correspond clairement, le nom apparaît tout seul.

Ce n'est pas de l'IA générative — juste de la géométrie (distance cosinus entre
empreintes). Léger, déterministe, 100% local. Stocké dans known_voices.json.

PRIORITÉ À LA PRÉCISION (objectif < 1% d'erreurs) : on n'auto-nomme QUE si la
similarité dépasse un seuil ÉLEVÉ *et* devance la 2e meilleure d'une marge. En
cas de doute on ne nomme pas (« Locuteur N » reste) — mieux vaut s'abstenir que
mal attribuer.
"""

import json
import math
import os
import threading

_LOCK = threading.Lock()

# Cosinus entre centroïdes L2 (CAM++ : même locuteur ~0,6-0,85 ; locuteurs
# différents < 0,4). Seuil haut + marge sur la 2e meilleure = quasi zéro faux
# positif.
# v1.0.23 : 0,62 -> 0,75. Mesuré sur une réunion réelle de 4 personnes dont une
# seule était enrôlée : la VRAIE correspondance sort à 0,877, tandis qu'une
# personne ABSENTE de la réunion atteignait 0,666 sur un autre locuteur — au-
# dessus de l'ancien seuil, donc affichée comme un nom certain. C'est le cas
# « timbres qui se ressemblent » : à 0,62 on nomme des gens au hasard. L'écart
# entre un vrai match (~0,88) et un faux (~0,67) est net ; 0,75 passe entre les
# deux. Conséquence assumée : moins de noms proposés, mais ceux qui le sont
# tiennent. Un « Locuteur 2 » honnête vaut mieux qu'un prénom faux.
MATCH_MIN = 0.75
# v29 : 0.06 -> 0.12. Un nom FAUX (locuteurs inversés) est bien pire qu'un
# « Locuteur N » générique — surtout pour le compte rendu SLM. On exige donc que
# la meilleure voix connue DEVANCE NETTEMENT la 2e ; sinon (ambiguïté, données
# d'enrôlement entremêlées) on n'attribue PAS de nom. Fail-safe > fail-wrong.
MATCH_MARGIN = 0.12
# Enrôlement : centroïde jugé fiable seulement au-delà de ce temps de parole.
ENROLL_MIN_SECONDS = 12.0


def _path(support_dir):
    return os.path.join(support_dir, "known_voices.json")


def load(support_dir):
    """Liste des voix connues : [{id, name, centroid[192], seconds, meetings}].
    Fichier absent -> []. Fichier présent mais corrompu -> on le met de côté en
    .corrupt (pour ne pas l'écraser silencieusement à l'enrôlement suivant et
    perdre les voix) puis [].
    """
    p = _path(support_dir)
    if not os.path.exists(p):
        return []
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, list) else []
    except Exception as e:
        try:
            os.replace(p, p + ".corrupt")
            print(f"[voiceid] known_voices.json illisible ({e}) -> sauvegardé "
                  f"en {os.path.basename(p)}.corrupt")
        except Exception:
            pass
        return []


def _save(support_dir, voices):
    """Écriture ATOMIQUE (fichier temporaire + os.replace) : un crash en cours
    d'écriture ne peut pas laisser un known_voices.json tronqué/corrompu."""
    try:
        os.makedirs(support_dir, exist_ok=True)
        p = _path(support_dir)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(voices, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
        return True
    except Exception as e:
        print(f"[voiceid] sauvegarde KO : {e}")
        return False


def _cos(a, b):
    # a et b sont L2-normalisés -> produit scalaire = cosinus.
    if not a or not b or len(a) != len(b):
        return -1.0
    return sum(x * y for x, y in zip(a, b))


def match(centroid, voices, exclude_ids=None):
    """Cherche la voix connue correspondante. Renvoie (voix, score) si match
    confiant (>= MATCH_MIN et devançant la 2e d'au moins MATCH_MARGIN), sinon
    (None, meilleur_score). exclude_ids : voix déjà attribuées dans la réunion
    (une même personne ne peut pas être deux locuteurs)."""
    exclude_ids = exclude_ids or set()
    # v1.0.23 — on IGNORE les entrées d'attente déjà présentes en base (« Voix 1 »,
    # « Locuteur 2 »…). Elles ont été enrôlées par des versions antérieures et
    # écrasaient les vraies identités avec une similarité de 1,000 (même vecteur).
    # On ne les supprime pas — l'utilisateur peut les renommer dans les Réglages,
    # et elles redeviennent alors des identités valides.
    scored = sorted(
        ((v, _cos(centroid, v.get("centroid"))) for v in voices
         if v.get("id") not in exclude_ids and not _is_placeholder_name(v.get("name"))),
        key=lambda t: t[1], reverse=True)
    if not scored:
        return None, 0.0
    best, bs = scored[0]
    second = scored[1][1] if len(scored) > 1 else -1.0
    if bs >= MATCH_MIN and (bs - second) >= MATCH_MARGIN:
        return best, bs
    return None, bs


def _new_id(voices):
    n = 0
    for v in voices:
        try:
            n = max(n, int(str(v.get("id", "")).split("_")[-1]))
        except Exception:
            pass
    return "voice_%03d" % (n + 1)


def _is_placeholder_name(name: str) -> bool:
    """v1.0.23 — « Voix 3 », « Locuteur 2 », « Speaker 01 », « Interlocuteur »…
    sont des étiquettes D'ATTENTE affichées quand on ne sait pas qui parle, pas
    des identités. On refuse de les enrôler (cf. enroll_or_update)."""
    import re
    n = (name or "").strip().lower()
    if not n:
        return True
    return bool(re.fullmatch(
        r"(voix|locuteur|speaker|intervenant|personne|participant)\s*[-_ ]?\d*"
        r"|interlocuteur|inconnu|unknown", n))


def enroll_or_update(support_dir, name, centroid, seconds, voice_id=None):
    """Enregistre / met à jour une voix connue. Si la voix existe (id fourni ou
    même nom), son centroïde est affiné par moyenne pondérée par le temps de
    parole puis re-normalisé (plus on l'entend, plus l'empreinte est juste).
    Renvoie l'id, ou None si nom vide / pas assez de secondes."""
    name = (name or "").strip()
    if not name or not centroid or float(seconds) < ENROLL_MIN_SECONDS:
        return None
    # v1.0.23 — NE JAMAIS MÉMORISER UN NOM D'ATTENTE. Constaté en base :
    # « Voix 1 » (551,54 s) et « Voix 2 » (1400,78 s) avaient été enrôlées, avec
    # exactement les durées de deux locuteurs d'une réunion. Conséquence : à la
    # réunion suivante, ces clones anonymes obtenaient une similarité de 1,000
    # (c'est le même vecteur) et battaient systématiquement la vraie identité —
    # « Raphael », pourtant à 0,877 sur le bon locuteur, n'était jamais affiché.
    # Une étiquette d'attente n'identifie personne : la mémoriser ne fait que
    # polluer la base et rendre la reconnaissance nominale impossible.
    if _is_placeholder_name(name):
        return None
    # Rejette une empreinte dégénérée (norme L2 ~0) : elle se normaliserait en
    # zéros et corromprait la reconnaissance (matchs faux). Une vraie empreinte
    # CAM++ L2-normalisée a une norme ~1.
    if sum(float(x) * float(x) for x in centroid) < 1e-8:
        return None
    with _LOCK:
        voices = load(support_dir)
        v = None
        if voice_id:
            v = next((x for x in voices if x.get("id") == voice_id), None)
        if v is None:
            v = next((x for x in voices
                      if x.get("name", "").lower() == name.lower()), None)
        if v is None:
            v = {"id": _new_id(voices), "name": name,
                 "centroid": [float(x) for x in centroid],
                 "seconds": float(seconds), "meetings": 1}
            voices.append(v)
        elif len(v.get("centroid", [])) != len(centroid):
            # Dimension différente (modèle d'empreinte changé) : on NE mélange
            # PAS (zip tronquerait silencieusement -> centroïde corrompu). La
            # nouvelle empreinte remplace l'ancienne.
            v["centroid"] = [float(x) for x in centroid]
            v["seconds"] = float(seconds)
            v["meetings"] = int(v.get("meetings", 1)) + 1
            v["name"] = name
        else:
            w0, w1 = float(v.get("seconds", 0.0)), float(seconds)
            tot = (w0 + w1) or 1.0
            merged = [(w0 * a + w1 * b) / tot
                      for a, b in zip(v["centroid"], centroid)]
            nrm = math.sqrt(sum(x * x for x in merged)) or 1.0
            v["centroid"] = [x / nrm for x in merged]
            v["seconds"] = w0 + w1
            v["meetings"] = int(v.get("meetings", 1)) + 1
            v["name"] = name
        # Ne confirme l'enrôlement (renvoi de l'id) que si la persistance a
        # réussi : sinon l'UI croirait la voix mémorisée alors qu'elle ne l'est pas.
        return v["id"] if _save(support_dir, voices) else None


def rename(support_dir, voice_id, name):
    name = (name or "").strip()
    if not name:
        return False
    with _LOCK:
        voices = load(support_dir)
        v = next((x for x in voices if x.get("id") == voice_id), None)
        if not v:
            return False
        v["name"] = name
        return _save(support_dir, voices)


def delete(support_dir, voice_id):
    with _LOCK:
        voices = load(support_dir)
        n = len(voices)
        voices = [x for x in voices if x.get("id") != voice_id]
        if len(voices) == n:
            return False
        return _save(support_dir, voices)


def public_list(support_dir):
    """Vue épurée pour l'UI (sans le centroïde, lourd et inutile à afficher)."""
    return [{"id": v.get("id"), "name": v.get("name", ""),
             "seconds": round(float(v.get("seconds", 0.0))),
             "meetings": int(v.get("meetings", 1))}
            for v in load(support_dir)]
