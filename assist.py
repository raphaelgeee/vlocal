"""assist.py, l'intelligence locale de Vlocal (aperçu).

Un modèle de langage local (Qwen3-4B-Instruct, MLX, 4 bits, 2,3 Go, licence
Apache 2.0) tourne sur le Mac pour ÉCRIRE quand la personne le demande : un
prompt, un courriel décrit (« rédige-moi un mail pour décaler la réunion »), une
traduction, un texte raccourci ou rendu formel, une réponse à un message copié.
Il extrait aussi un rappel d'une phrase et rédige le compte rendu d'une réunion.

La reconnaissance des demandes est dans intents.py (sans modèle), la simple mise
en forme d'un texte dicté dans mise_en_forme.py (sans modèle). Les règles :

1. Sans demande, la dictée reste au mot près et le modèle n'est pas réveillé.
2. Le modèle écrit à partir de ce que la personne dit, jamais de ses propres
   connaissances ; chaque résultat passe des garde-fous (accepte_demande), sinon
   la dictée est insérée telle qu'elle a été dite.
3. Jamais sans borne : chaque appel a un délai maximal (budget_redaction).
4. Chargé à la demande, déchargé après inactivité.
5. Rien ne quitte le Mac. Le modèle est téléchargé une fois depuis R2, vérifié
   par empreinte, comme les modèles de transcription.

Ce module ne dépend d'aucun autre module de l'app : app.py le branche.
"""
import difflib
import json
import os
import re
import threading
import time

# v1.4.2 : Qwen3-4B-Instruct-2507. Banc de rédaction sur 22 dictées réelles : le LFM2.5-1.2B
# inventait (« une voiture de police poursuit un avion » pour un train), le LFM2-2.6B perdait
# des détails d'un long prompt et inventait des circonstances ; le Qwen3-4B est resté fidèle
# sur 21 cas sur 22. Plus lent, mais la mise en forme ne passe plus par le modèle.
MODEL_NAME = "qwen3-4b-instruct-2507-mlx-4bit"    # clé du manifeste R2 (section « optional »)
MODEL_LABEL = "Qwen3-4B"
def _idle_unload_s():
    """Dix minutes sans usage, trente sur un Mac de 16 Go ou plus : le recharger coûte plusieurs
    secondes (8,6 s mesurées en économie d'énergie) et la mémoire y est moins rare."""
    try:
        import os as _os
        return 30 * 60 if _os.sysconf("SC_PAGE_SIZE") * _os.sysconf("SC_PHYS_PAGES") >= 15.5 * 2 ** 30 else 10 * 60
    except Exception:
        return 10 * 60


IDLE_UNLOAD_S = _idle_unload_s()
TIMEOUT_MEETING_S = 120.0

# --------------------------------------------------------------------------- #
# Consignes : rappels et comptes rendus
# --------------------------------------------------------------------------- #
# Le modèle ne calcule JAMAIS une date : le banc l'a montré incapable (« demain
# à dix heures » donnait aujourd'hui). Il dit seulement si c'est un rappel, quoi,
# et QUAND tel que dit ; le parseur déterministe de reminders.py fait le calcul.
RAPPEL_REGLE = ("Tu lis une phrase dictée en français ou en anglais. Réponds uniquement par un objet JSON : "
                '{"rappel": true, "titre": "l\'action à rappeler, courte, sans le verbe rappeler", "quand": "le moment exactement tel que dit"} '
                'si la phrase demande de rappeler quelque chose à un moment donné, sinon {"rappel": false}.')

# Réunion : par MORCEAUX. Un modèle de cette taille, face à une heure de
# transcription, écrit un résumé générique et invente. Sur trois minutes, il
# extrait juste. On extrait donc morceau par morceau, en JSON, puis on fusionne
# sans lui : c'est nous qui rédigeons le compte rendu final, pas le modèle.
CR_CHUNK_REGLE = ("Tu lis un extrait de transcription de réunion ; chaque ligne commence par le locuteur. Réponds uniquement par un objet JSON : "
                  '{"decisions": ["…"], "actions": [{"qui": "…", "quoi": "…", "quand": "…"}], "sujets_ouverts": ["…"], '
                  '"noms": [{"locuteur": "Locuteur 2", "prenom": "Gaspard"}]}. '
                  "Une décision est quelque chose d'acté par le groupe ; une action, quelque chose que quelqu'un fera ; un sujet ouvert, "
                  "une question laissée sans réponse ; un nom, un prénom qu'un participant se donne (« moi c'est Julien ») ou par lequel "
                  "un autre l'appelle. Listes vides si rien. Uniquement ce qui est dit dans l'extrait, avec ses mots.")
CR_CHUNK_CHARS = 9000    # environ 2 500 tokens : la taille où l'extraction reste fidèle


# --------------------------------------------------------------------------- #
# Garde-fous sur le résultat
# --------------------------------------------------------------------------- #

def similarite(a, b):
    """Part des mots d'origine retrouvés, dans l'ordre, dans le résultat (0..1)."""
    wa = re.findall(r"\w+", (a or "").lower())
    wb = re.findall(r"\w+", (b or "").lower())
    if not wa or not wb:
        return 0.0
    return difflib.SequenceMatcher(None, wa, wb).ratio()


_PREAMBULES = re.compile(r"^\s*(voici|here is|here's|bien sûr|certainly|sure)[^\n]*\n", re.I)
# Le modèle recopie parfois l'étiquette de la consigne devant le titre.
_ETIQUETTES_TITRE = re.compile(r"(?im)^\s*(titre( court)?|title|sujet|objet)\s*:\s*")
_STOP = set("le la les un une des de du d l et ou à a au aux en dans sur pour par avec sans que qui quoi dont où ce cet cette ces "
            "se sa son ses leur leurs mon ma mes ton ta tes notre votre nos vos il elle ils elles on nous vous je tu me te y ne pas "
            "plus moins très bien mais donc car si est sont était sera être avoir fait faire the a an and or of to in on for with "
            "that this it is are was be have has do "
            # nombres en lettres : la consigne les passe en chiffres, ce n'est pas une perte
            "zéro deux trois quatre cinq sept huit neuf onze douze treize quatorze quinze seize vingt trente quarante cinquante "
            "soixante cent mille zero three four five seven eight nine eleven twelve thirteen fourteen fifteen sixteen twenty thirty "
            "forty fifty sixty hundred thousand".split())


def mots_pleins(texte):
    return [w for w in re.findall(r"\w+", (texte or "").lower()) if len(w) >= 4 and w not in _STOP]


def couverture(original, out):
    """Part des mots pleins du texte dicté que l'on retrouve dans le résultat (0..1)."""
    mo = mots_pleins(original)
    if not mo:
        return 1.0
    ms = set(mots_pleins(out))
    return sum(1 for w in mo if w in ms) / len(mo)


def nettoyer_sortie(out):
    """Retire un éventuel préambule et des guillemets d'encadrement."""
    s = (out or "").strip()
    s = _PREAMBULES.sub("", s, count=1).strip()
    s = _ETIQUETTES_TITRE.sub("", s, count=1).strip()
    if len(s) >= 2 and s[0] in "«\"“" and s[-1] in "»\"”":
        s = s[1:-1].strip()
    return s


_MOTS_EN = set("the and with for that this from your you are will can would should about into their there to is a of in on at my me "
                "i we our it be have has thanks thank hello hi please write email meeting".split())
_MOTS_FR = set("le la les des une et pour que qui dans avec sur pas est sont vous nous votre cette".split())


def langue_probable(texte):
    w = re.findall(r"[a-zàâçéèêëîïôûùüÿœ']+", (texte or "").lower())
    en = sum(1 for x in w if x in _MOTS_EN); fr = sum(1 for x in w if x in _MOTS_FR)
    if en == fr == 0:
        return None
    return "en" if en > fr * 1.5 else ("fr" if fr > en * 1.5 else None)


_META = re.compile(r"(?i)^\s*(voici|here (?:is|are)|bien sûr|certainly|sure|d'accord|ok[,!]|je vais|je peux|je comprends|i can|i will|i'll|"
                   r"je te propose|je vous propose|je corrige|je reformule|je résume|je traduis|réponse\s*:|answer\s*:)")


def nettoyer_commande(out):
    """Retire les lignes d'annonce (« Voici … : », « Je vais t'aider … ») en tête d'une réponse
    et l'étiquette « Réponse : » que l'exemple a pu induire."""
    lignes = (out or "").strip().split("\n")
    while lignes and _META.match(lignes[0]):
        premiere = lignes.pop(0)
        # « Here are three names: A, B, C. » : le contenu suit le deux-points sur la même ligne
        if ":" in premiere and not premiere.rstrip().endswith(":"):
            lignes.insert(0, premiere.split(":", 1)[1].strip())
            break
        if not lignes:
            lignes = [premiere]      # rien d'autre : on laisse le garde-fou trancher
            break
    s = "\n".join(lignes).strip()
    return re.sub(r"(?i)^(réponse|answer)\s*:\s*", "", s)


def repetitif(out):
    """Une liste dont plus de 40 % des lignes commencent par les mêmes deux mots tourne en rond."""
    lignes = [re.sub(r"^[\s\-\*\d\.\)]+", "", l).strip().lower() for l in (out or "").split("\n")]
    lignes = [l for l in lignes if l]
    if len(lignes) < 6:
        return False
    tetes = {}
    for l in lignes:
        k = re.sub(r"\W+$", "", l.split()[0]) if l.split() else ""
        tetes[k] = tetes.get(k, 0) + 1
    return max(tetes.values()) / len(lignes) > 0.4 or len(lignes) > 40


def rappel_depuis_json(out):
    """Valide la sortie JSON du modèle : {rappel, titre, quand}. None si inutilisable.
    Le calcul de la date est laissé au parseur déterministe (reminders.parse_datetime)."""
    s = (out or "").strip()
    m = re.search(r"\{.*\}", s, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except Exception:
        return None
    if not isinstance(d, dict) or not d.get("rappel"):
        return None
    titre = str(d.get("titre") or "").strip().strip(".")
    quand = str(d.get("quand") or "").strip()
    if not titre or not quand or len(titre) > 140 or len(quand) > 80:
        return None
    if re.match(r"(?i)^(rappel|remind|n'oublie|pense à|penser à)", titre):
        titre = re.sub(r"(?i)^(rappelle[- ]?(moi|nous)\s*(de|d')?\s*|rappeler\s*(de|d')?\s*|rappel\s*(de|d'|:)?\s*|remind me to\s*|"
                       r"n'oublie pas\s*(de|d')?\s*|pense à\s*|penser à\s*)", "", titre).strip() or titre
    return {"titre": titre[0].upper() + titre[1:], "quand": quand}


_HEURE_MOTS = re.compile(r"(?i)(?<!dans )(?<!pendant )\b(un|une|deux|trois|quatre|cinq|six|sept|huit|neuf|dix|onze|douze|treize|quatorze|quinze|seize|"
                         r"dix-sept|dix-huit|dix-neuf|vingt|vingt-et-un|vingt-deux|vingt-trois)\s+heures?(?:\s+(trente|quinze|quarante-cinq|\d{1,2}))?\b")
_MOTS_NB = {"un": 1, "une": 1, "deux": 2, "trois": 3, "quatre": 4, "cinq": 5, "six": 6, "sept": 7, "huit": 8, "neuf": 9, "dix": 10,
            "onze": 11, "douze": 12, "treize": 13, "quatorze": 14, "quinze": 15, "seize": 16, "dix-sept": 17, "dix-huit": 18,
            "dix-neuf": 19, "vingt": 20, "vingt-et-un": 21, "vingt-deux": 22, "vingt-trois": 23, "trente": 30, "quarante-cinq": 45}


def chiffrer_heures(texte):
    """« dix-sept heures trente » -> « 17h30 » : le parseur de dates lit les chiffres."""
    def _r(m):
        h = _MOTS_NB.get(m.group(1).lower())
        mn = m.group(2)
        mn = _MOTS_NB.get(mn.lower(), None) if mn and not mn.isdigit() else (int(mn) if mn else 0)
        if h is None or mn is None:
            return m.group(0)
        return "%dh%02d" % (h, mn) if mn else "%dh" % h
    return _HEURE_MOTS.sub(_r, texte or "")


def resoudre_rappel(extrait, texte, now):
    """Rappel hybride : le modèle a extrait {titre, quand}, le parseur déterministe date.
    Essais dans l'ordre : « quand » tel quel, « dans quand » si le texte disait « dans »,
    puis la phrase entière. Anglais ramené en français avant. Le titre du modèle est gardé
    s'il reprend le texte, sinon on repart du titre déterministe.
    Retourne {titre, when (datetime), label, confidence} ou None."""
    import reminders as _rem
    if not extrait:
        return None
    quand = chiffrer_heures(_rem.en_to_fr_time(extrait.get("quand") or ""))
    phrase = chiffrer_heures(_rem.en_to_fr_time(texte or ""))
    essais = []
    if re.search(r"(?i)\bdans\b", phrase) and not re.match(r"(?i)^\s*dans\b", quand):
        essais.append("dans " + _rem.en_to_fr_time(extrait.get("quand") or ""))
    essais += [quand, phrase]
    for cand in essais:
        if not cand.strip():
            continue
        try:
            r = _rem.parse_datetime(cand, now)
        except Exception:
            r = None
        if r and r[0]:
            titre = (extrait.get("titre") or "").strip()
            # Le titre doit venir du texte : au moins la moitié de ses mots pleins s'y retrouvent.
            if titre and couverture(titre, texte) < 0.5:
                titre = ""
            if not titre:
                try:
                    titre = _rem._extract_title(texte) or ""
                except Exception:
                    titre = ""
            titre = re.sub(r"(?i)^(don'?t forget to|remind me to|remember to|please)\s+", "", titre)
            titre = re.sub(r"(?i)\s*\b(demain|ce soir|aujourd'hui|tomorrow|tonight|today)\b.*$", "", titre).strip(" ,.") or titre
            # l'échéance n'a rien à faire dans le titre (« Call the accountant on Monday at 9 »)
            titre = re.sub(r"(?i)\s+(?:(?:on|at|by|before|next|le|à|a|avant|vers|dès|dans)\s+)+"
                           r"(?:lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche|monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
                           r"\d{1,2}(?:[h:]\d{0,2})?|midi|minuit|noon)\b.*$", "", titre).strip(" ,.") or titre
            if not titre:
                return None
            return {"titre": titre[0].upper() + titre[1:], "when": r[0], "label": r[1], "confidence": r[2]}
    return None


# --------------------------------------------------------------------------- #
# v1.4.2 : RÉDACTION À PARTIR D'UNE DEMANDE RECONNUE (intents.detect)
#
# Deux modes. FORMAT : la dictée est le texte, on le met en forme en gardant ses
# mots et toutes ses informations. COMPOSE : la dictée décrit ce qu'il faut
# écrire, on le rédige sans rien inventer. Les exemples sont donnés au modèle
# comme de vrais échanges : c'est ce qui tient un petit modèle au format demandé.
# --------------------------------------------------------------------------- #
REGLE_REDACTION = (
    "Tu es l'assistant d'écriture de la personne. Elle te dicte à voix haute et tu produis exactement ce qu'elle "
    "demande, prêt à être collé : aucune introduction, aucun commentaire, aucune phrase du type « voici », pas de "
    "guillemets autour du résultat. Tu écris dans la langue de la dictée, sauf si une autre langue est demandée. Tu "
    "gardes son tutoiement ou son vouvoiement. Tu n'inventes aucun fait, aucun nom, aucun chiffre, aucune date, aucun "
    "lien qui ne soit pas dans la dictée, et aucune circonstance : pas d'échange précédent, de demande ou d'intérêt que la "
    "personne n'a pas mentionné.")

NOMS_LIVRABLES = {"mail": "courriel", "prompt": "prompt", "message": "message", "note": "note", "liste": "liste",
                  "post": "publication LinkedIn", "reponse": "réponse"}
LANGUES_NOMS = {"en": "anglais", "fr": "français", "es": "espagnol", "de": "allemand", "it": "italien"}

_CONSIGNE_FORMAT = {
    "mail": "Mets ce texte en forme de courriel{dest} : une salutation{salut}, le contenu en paragraphes courts avec ses mots et toutes ses "
            "informations, sans les hésitations de l'oral, puis une formule de politesse courte. N'ajoute aucune information.",
    "message": "Mets ce texte en forme de message court{dest}, direct, avec ses mots et toutes ses informations, sans formule longue.",
    "note": "Mets ce texte en forme de note : un titre de quelques mots sur la première ligne, puis une puce « - » par idée, avec ses mots. "
            "N'ajoute rien.",
    "liste": "Mets ce texte en liste à puces : une puce « - » par élément, avec ses mots. Rien d'autre que la liste.",
    "prompt": "Transforme ce texte en prompt pour une IA, écrit à la deuxième personne (« Tu… », « Rédige… »). Reprends tous les éléments "
              "demandés, dans l'ordre, sans en retirer ni en ajouter : le contexte, ce qu'il faut produire, puis les contraintes et le "
              "format attendu seulement s'ils sont dictés. N'invente aucune contrainte ni aucun format.",
    "post": "Mets ce texte en forme de publication LinkedIn : une première phrase courte qui accroche, puis des paragraphes d'une ou deux "
            "phrases séparés par une ligne vide, avec ses mots et ses chiffres. Pas de hashtag, rien d'inventé.",
}
_CONSIGNE_COMPOSE = {
    "mail": "Rédige le courriel décrit par la demande{dest} : une salutation{salut}, un corps court et concret, une formule de politesse courte. "
            "Uniquement ce que dit la demande ; si une information manque (date, prix, lien), ne l'invente pas.",
    "message": "Rédige le message décrit par la demande{dest} : court, direct, naturel. Uniquement ce que dit la demande.",
    "note": "Rédige la note décrite par la demande : un titre de quelques mots, puis une puce « - » par idée. Uniquement ce que dit la demande.",
    "liste": "Rédige la liste décrite par la demande : une puce « - » par élément, concrète. Rien d'autre que la liste.",
    "prompt": "Rédige le prompt décrit par la demande, prêt à coller dans une IA. Pour une image : une description visuelle précise en une ou "
              "deux phrases (sujet, action, décor, lumière, cadrage, style). Pour un texte : à la deuxième personne, le rôle, la tâche, les "
              "contraintes et le format attendu. Ne réponds pas toi-même à la tâche : écris seulement le prompt.",
    "post": "Rédige la publication LinkedIn décrite par la demande : une première phrase qui accroche, des paragraphes courts séparés par "
            "une ligne vide. Pas de hashtag, aucun chiffre inventé.",
    "reponse": "Rédige la réponse au message reçu, à partir de ce que dit la personne : dans la langue du message reçu, une salutation, "
               "sa réponse, une formule de politesse courte. N'ajoute aucune information.",
    "commande": "Réponds à la demande par le résultat demandé, et seulement lui.",
}

# Longueur maximale par livrable rédigé (jetons) : court, c'est aussi plus rapide
_MAX_COMPOSE = {"message": 160, "mail": 360, "prompt": 360, "liste": 300, "note": 320, "post": 450, "reponse": 400, "commande": 500}

# Exemples (vrais échanges) : un par livrable et par mode. Courts : ils coûtent du temps de lecture.
_EX = {
    ("mail", "format"): ("Texte dicté :\nbah merci pour ton retour euh sur le devis, c'est bon pour nous, par contre on aimerait décaler la livraison "
                         "à la semaine du douze, tu me dis si c'est possible\n\nDemande : Mets ce texte en forme de courriel pour Thomas : une "
                         "salutation avec son prénom, le contenu en paragraphes courts avec ses mots et toutes ses informations, sans les "
                         "hésitations de l'oral, puis une formule de politesse courte. N'ajoute aucune information.",
                         "Bonjour Thomas,\n\nMerci pour ton retour sur le devis, c'est bon pour nous.\n\nPar contre, on aimerait décaler la "
                         "livraison à la semaine du 12. Tu me dis si c'est possible ?\n\nBonne journée,"),
    ("mail", "compose"): ("Demande : écris un mail à Sophie pour reporter notre point de mardi à jeudi même heure\n\nConsigne : Rédige le "
                          "courriel décrit par la demande pour Sophie : une salutation avec son prénom, un corps court et concret, une formule "
                          "de politesse courte. Uniquement ce que dit la demande ; si une information manque (date, prix, lien), ne l'invente pas.",
                          "Bonjour Sophie,\n\nJe dois reporter notre point de mardi. Est-ce que jeudi, à la même heure, te conviendrait ?\n\n"
                          "Merci et bonne journée,"),
    ("prompt", "format"): ("Texte dicté :\nalors je veux que tu me fasses un résumé des retours clients du mois, tu vois, classés par thème, avec "
                           "les trois problèmes qui reviennent le plus, et à la fin un tableau avec le thème et le nombre de retours, pas de "
                           "blabla\n\nDemande : Transforme ce texte en prompt pour une IA, écrit à la deuxième personne (« Tu… », « Rédige… »). "
                           "Reprends tous les éléments demandés, dans l'ordre, sans en retirer ni en ajouter : le contexte, ce qu'il faut "
                           "produire, puis les contraintes et le format attendu seulement s'ils sont dictés. N'invente aucune contrainte ni aucun format.",
                           "Tu vas analyser les retours clients du mois.\n\nTâche : rédige un résumé de ces retours, classés par thème, en "
                           "faisant ressortir les trois problèmes qui reviennent le plus.\n\nContraintes : va à l'essentiel, sans blabla.\n\n"
                           "Format attendu : termine par un tableau avec deux colonnes, le thème et le nombre de retours."),
    ("prompt", "compose"): ("Demande : fais-moi un prompt pour générer une image d'un chat qui lit le journal\n\nConsigne : Rédige le prompt "
                            "décrit par la demande, prêt à coller dans une IA. Pour une image : une description visuelle précise en une ou deux "
                            "phrases (sujet, action, décor, lumière, cadrage, style). Pour un texte : à la deuxième personne, le rôle, la tâche, "
                            "les contraintes et le format attendu. Ne réponds pas toi-même à la tâche : écris seulement le prompt.",
                            "Un chat roux assis dans un fauteuil en cuir, lunettes sur le nez, lisant un journal déplié, lumière chaude du matin "
                            "par la fenêtre, plan moyen, style photo réaliste, 50 mm."),
    ("liste", "format"): ("Texte dicté :\nfaut que je rappelle le garage, que je paie la cantine et euh que je réserve le restau pour samedi\n\n"
                          "Demande : Mets ce texte en liste à puces : une puce « - » par élément, avec ses mots. Rien d'autre que la liste.",
                          "- Rappeler le garage\n- Payer la cantine\n- Réserver le restaurant pour samedi"),
    ("message", "format"): ("Texte dicté :\nbon je vais avoir un quart d'heure de retard le métro est bloqué commencez sans moi\n\nDemande : Mets ce "
                            "texte en forme de message court, direct, avec ses mots et toutes ses informations, sans formule longue.",
                            "J'aurai un quart d'heure de retard, le métro est bloqué. Commencez sans moi."),
    ("note", "format"): ("Texte dicté :\nappel avec Karim, il valide le budget, il veut la maquette pour le quinze et il faut prévoir une démo "
                         "pour son directeur\n\nDemande : Mets ce texte en forme de note : un titre de quelques mots sur la première ligne, puis "
                         "une puce « - » par idée, avec ses mots. N'ajoute rien.",
                         "Appel avec Karim\n- Il valide le budget\n- Il veut la maquette pour le 15\n- Prévoir une démo pour son directeur"),
    ("commande", "compose"): ("Demande : donne-moi trois synonymes de lent\n\nConsigne : Réponds à la demande par le résultat demandé, et seulement lui.",
                              "paresseux, traînant, poussif"),
}


def _placeholders(consigne, d, ctx):
    dest = (d.get("dest") or "").strip()
    return (consigne.replace("{dest}", (" pour " + dest) if dest else "")
            .replace("{salut}", " avec son prénom" if dest and dest[:1].isupper() else ""))


def consigne_demande(d, ctx=None):
    """(system, user, exemples, max_tokens) pour une demande reconnue."""
    ctx = ctx or {}
    kind, liv, mode = d.get("kind"), d.get("livrable"), d.get("mode")
    contenu = (d.get("contenu") or "").strip()
    source = (ctx.get("contexte") or "").strip()
    exemples = []
    if kind == "transforme":
        parts = []
        if d.get("langue") and d.get("langue") != "auto":
            parts.append("Traduis ce texte en %s, naturellement." % LANGUES_NOMS.get(d["langue"], d["langue"]))
        elif d.get("langue") == "auto":
            parts.append("Traduis ce texte en %s, naturellement." % ("français" if langue_probable(source or contenu) == "en" else "anglais"))
        if d.get("court"):
            parts.append("Raccourcis-le de moitié en gardant toutes les informations concrètes : noms, dates, chiffres, décisions.")
        if d.get("formel"):
            parts.append("Réécris-le dans un registre formel et professionnel, avec exactement les mêmes informations.")
        if d.get("reformule"):
            parts.append("Reformule-le plus clairement, avec exactement les mêmes informations.")
        texte = source if d.get("source") == "presse-papiers" else contenu
        user = "Texte :\n" + texte[:6000] + "\n\nDemande : " + " ".join(parts or ["Mets ce texte au propre."])
        return REGLE_REDACTION, user, [], min(1400, max(120, int(len(texte.split()) * 2.2) + 80))
    if kind == "commande":
        user = "Demande : " + contenu + "\n\nConsigne : " + _CONSIGNE_COMPOSE["commande"]
        return REGLE_REDACTION, user, [_EX[("commande", "compose")]], _MAX_COMPOSE["commande"]
    if kind == "reponse":
        user = ("Message reçu :\n" + source[:5000] + "\n\nCe que la personne veut répondre : " + contenu
                + "\n\nConsigne : " + _CONSIGNE_COMPOSE["reponse"])
        return REGLE_REDACTION, user, [], _MAX_COMPOSE["reponse"]
    if mode == "format":
        consigne = _placeholders(_CONSIGNE_FORMAT.get(liv, _CONSIGNE_FORMAT["note"]), d, ctx)
        user = "Texte dicté :\n" + contenu + "\n\nDemande : " + consigne
        n = len(contenu.split())
        max_tokens = min(1600, max(120, int(n * (2.6 if liv in ("prompt", "post", "mail") else 2.0)) + 120))
    else:
        consigne = _placeholders(_CONSIGNE_COMPOSE.get(liv, _CONSIGNE_COMPOSE["commande"]), d, ctx)
        user = "Demande : " + contenu + "\n\nConsigne : " + consigne
        max_tokens = _MAX_COMPOSE.get(liv, 500)
    if d.get("langue") and d.get("langue") != "auto":
        user += " Écris le résultat en %s." % LANGUES_NOMS.get(d["langue"], d["langue"])
    elif langue_probable(contenu) == "en":
        user += " The request is in English: write the result in English."
    if d.get("court"):
        user += " Fais court."
    if d.get("formel"):
        user += " Registre formel, vouvoiement."
    ex = _EX.get((liv, mode))
    if ex:
        exemples.append(ex)
    return REGLE_REDACTION, user, exemples, max_tokens


_SEUILS_FORMAT = {"mail": 0.6, "message": 0.6, "note": 0.6, "liste": 0.6, "prompt": 0.55, "post": 0.5}
_FUITES_REDAC = re.compile(r"(?i)(texte dicté|la dictée|cette dictée|this dictation|ce texte dicté|consigne|la demande|demande :|texte :|"
                           r"n'ajoute aucune|avec ses mots|sans les hésitations|prêt à coller|ligne vide|deuxième personne|je vais (?:t'|vous )?aider|"
                           r"en tant qu'(?:ia|assistant)|as an ai)")


def noms_propres(texte):
    """Mots à majuscule hors début de phrase (prénoms, marques, sigles) : Marie, Miria, LinkedIn, LTC."""
    out = []
    for m in re.finditer(r"\b([A-ZÀ-Ý][a-zà-ÿ]+(?:-[A-ZÀ-Ý][a-zà-ÿ]+)*|[A-Z]{2,}[a-z]?)\b", texte or ""):
        avant = (texte or "")[:m.start()].rstrip()
        if not avant or avant[-1] in ".!?:«\"":
            continue
        if m.group(1) not in out:
            out.append(m.group(1))
    return out


def _mots_sujet(texte):
    import intents as _it
    return [w for w in mots_pleins(texte) if not _it.livrable_de(w) and not re.match(
        r"(?i)^(redige|rédige|rediger|rédiger|ecris|écris|ecrire|écrire|fais|faire|prepare|prépare|genere|génère|cree|crée|propose|donne|"
        r"peux|pourrais|veux|voudrais|aimerais|souhaite|souhaiterais|besoin|faut|objectif|write|draft|make|create|could|would|please|"
        r"petit|petite|court|courte|rapide|juste|vraiment|gros|direct|directement)$", w)]


def accepte_demande(d, out, ctx=None):
    """(ok, info) : le résultat est-il digne d'être inséré à la place de la dictée ?"""
    ctx = ctx or {}
    s = nettoyer_commande(nettoyer_sortie(out or ""))
    if not s or len(mots_pleins(s)) < 1:
        return False, "vide", s
    kind, liv, mode = d.get("kind"), d.get("livrable"), d.get("mode")
    contenu = d.get("contenu") or ""
    source = (ctx.get("contexte") or "").strip()
    ref = source if d.get("source") == "presse-papiers" else contenu
    if re.search(r"(?i)^(je ne peux pas|i can(?:'|no)t|désolé|sorry|as an ai|en tant qu'ia)", s):
        return False, "refus", s
    fuite = _FUITES_REDAC.search(s)
    if fuite and not _FUITES_REDAC.search(ref):
        return False, "consigne recopiée (« %s »)" % fuite.group(0), s
    if _META.match(s):
        return False, "réponse méta", s
    if repetitif(s):
        return False, "répétitif", s
    if len(s) > 4000:
        return False, "trop long", s
    traduit = d.get("langue") and d.get("langue") not in ("auto",) and langue_probable(ref) not in (None, d.get("langue"))
    traduit = traduit or d.get("langue") == "auto"
    if not traduit:
        lr, lo = langue_probable(ref), langue_probable(s)
        if lr and lo and lr != lo:
            return False, "langue différente (%s -> %s)" % (lr, lo), s
    # le tutoiement ou le vouvoiement dicté est gardé (sauf demande de registre formel)
    import mise_en_forme as _mf
    if not traduit and not d.get("formel") and kind != "reponse":
        r_in, r_out = _mf.registre(ref), _mf.registre(s)
        if r_in and r_out and r_in != r_out:
            return False, "registre changé (%s -> %s)" % (r_in, r_out), s
    if kind == "transforme":
        if traduit:
            if len(s) < len(ref) * 0.45 or len(s) > len(ref) * 2.2 + 40:
                return False, "longueur incohérente pour une traduction", s
            return True, "ok", s
        cv = couverture(ref, s)
        if d.get("court"):
            if len(s) >= len(ref):
                return False, "pas plus court", s
            return (cv >= 0.3, "couverture %.2f" % cv, s)
        return (cv >= 0.45, "couverture %.2f" % cv, s)
    if mode == "format":
        if liv == "prompt" and not traduit:
            s = elaguer_ajouts(ref, s)
        if traduit:
            if len(s) < len(ref) * 0.4 or len(s) > len(ref) * 3 + 300:
                return False, "longueur incohérente", s
            return True, "ok (traduit)", s
        cv = couverture(ref, s)
        seuil = _SEUILS_FORMAT.get(liv, 0.6) * (0.6 if d.get("court") else 1.0)
        if cv < seuil:
            return False, "contenu perdu (couverture %.2f)" % cv, s
        if len(s) > len(ref) * 3 + 300:
            return False, "rallongé", s
        return True, "ok, couverture %.2f" % cv, s
    # compose / commande / réponse : la réponse doit parler du sujet demandé, sans le recopier
    if similarite(contenu, s) > 0.8:
        return False, "demande recopiée", s
    sujet = _mots_sujet(contenu)
    if kind != "reponse" and len(sujet) >= 2:
        ms = set(mots_pleins(s))
        n = sum(1 for w in sujet if w in ms or any(x.startswith(w[:5]) for x in ms))
        # une liste ou une note peut ne pas répéter le mot de la demande (« courses ») ; un prompt, un mail doivent tenir le sujet
        seuil = 0.34 if liv in ("liste", "note", "post") or kind == "commande" else 0.5
        if n == 0 or n / len(sujet) < seuil:
            return False, "hors sujet (%d/%d mots de la demande)" % (n, len(sujet)), s
        # les noms propres de la demande (Marie, Miria, LinkedIn) doivent se retrouver dans le résultat
        for np_ in noms_propres(contenu):
            # l'IA à qui le prompt est destiné n'a pas à être nommée dans le prompt
            if re.match(r"(?i)^(claude|chatgpt|gpt|gemini|mistral|perplexity|copilot|midjourney|dall-e|ia|ai|llm)$", np_):
                continue
            if np_.lower() not in s.lower():
                return False, "nom propre perdu (%s)" % np_, s
    return True, "ok", s


def economie_energie():
    """True si macOS est en mode économie d'énergie (la puce graphique y tourne environ deux fois moins vite)."""
    try:
        from Foundation import NSProcessInfo
        return bool(NSProcessInfo.processInfo().isLowPowerModeEnabled())
    except Exception:
        return False


def budget_redaction(max_tokens):
    """Délai accordé au modèle : de quoi écrire max_tokens à 25 jetons/s, plus la lecture, doublé en
    économie d'énergie ; jamais plus de 45 s (au-delà, la dictée est rendue telle quelle)."""
    b = 3.0 + max_tokens / 25.0
    if economie_energie():
        b *= 2.0
    return min(45.0, b)


def rediger_sans_modele(d, ctx=None):
    """Mise en forme déterministe (mise_en_forme.py) quand la demande s'y prête : un livrable
    en mode FORMAT, sans traduction ni réécriture demandée. None sinon."""
    import mise_en_forme as _mf
    if d.get("kind") != "livrable" or d.get("mode") != "format":
        return None
    if d.get("langue") or d.get("court") or d.get("formel"):
        return None
    return _mf.mettre_en_forme(d.get("livrable"), d.get("contenu") or "", d.get("dest") or "", (ctx or {}).get("signature") or "")


_ETIQ_PROMPT = re.compile(r"(?im)^\s*(contexte|tâche|tache|contraintes?|format attendu|format|rôle|role|objectif|context|task|constraints?|"
                          r"expected format|output format|goal)\s*:\s*")


def elaguer_ajouts(ref, texte, seuil=0.35):
    """Retire d'un texte mis en forme les paragraphes que la dictée ne contient pas (au-delà du premier) :
    un « Contraintes : garder un langage simple » que personne n'a dicté est une invention."""
    paras = [p for p in re.split(r"\n\s*\n", texte or "") if p.strip()]
    if len(paras) <= 1:
        return texte
    base = set(mots_pleins(ref))
    garde = [paras[0]]
    for p in paras[1:]:
        mots = [w for w in mots_pleins(_ETIQ_PROMPT.sub("", p)) if w not in ("dois", "doit", "vas", "va")]
        if not mots:
            continue
        part = sum(1 for w in mots if w in base or any(x.startswith(w[:5]) for x in base)) / len(mots)
        if part >= seuil:
            garde.append(p)
    return "\n\n".join(garde)


def signer(texte, d, ctx):
    """Signature ajoutée de façon déterministe à un courriel ou une réponse, si elle n'y est pas."""
    sig = ((ctx or {}).get("signature") or "").strip()
    if d.get("livrable") in ("mail", "reponse"):
        # le destinataire placé en signature (« Cordialement, Nabil ») est une erreur de modèle : retiré
        lignes = texte.rstrip().split("\n")
        der = lignes[-1].strip() if lignes else ""
        if der and re.match(r"^[A-ZÀ-Ý][\w'-]+(?: [A-ZÀ-Ý][\w'-]+)?$", der) and der.lower() != sig.lower() \
                and der.lower() in "\n".join(lignes[:3]).lower():
            texte = "\n".join(lignes[:-1]).rstrip()
    if not sig or d.get("livrable") not in ("mail", "reponse"):
        return texte
    if sig.lower() in texte[-120:].lower():
        return texte
    return texte.rstrip() + "\n" + sig


# --------------------------------------------------------------------------- #
# Le modèle : chargement paresseux, borne de temps, déchargement
# --------------------------------------------------------------------------- #
class Assist:
    """Un modèle, un verrou, un délai. Toute erreur devient None : l'appelant
    garde le brut."""

    def __init__(self, model_dir_fn, log=print):
        self._model_dir_fn = model_dir_fn
        self._log = log
        self._lock = threading.Lock()
        self._model = None
        self._tok = None
        self.last_use = 0.0
        self.loading = False
        self.last_error = ""
        self.last_latency_s = None

    # ---- disponibilité ---------------------------------------------------
    @staticmethod
    def importable():
        try:
            import importlib.util
            return importlib.util.find_spec("mlx_lm") is not None
        except Exception:
            return False

    def model_present(self):
        d = self._model_dir_fn()
        return bool(d) and os.path.exists(os.path.join(d, "config.json")) and any(
            f.endswith(".safetensors") for f in os.listdir(d))

    def loaded(self):
        return self._model is not None

    def load(self):
        """Charge le modèle (2 à 4 s). Idempotent ; sûr depuis n'importe quel thread."""
        with self._lock:
            if self._model is not None:
                return True
            if not self.model_present():
                self.last_error = "modèle absent"
                return False
            self.loading = True
            try:
                t = time.time()
                from mlx_lm import load as _load
                self._model, self._tok = _load(self._model_dir_fn())
                self.last_use = time.time()
                self._log("[assist] modèle %s chargé en %.1f s." % (MODEL_LABEL, time.time() - t))
                self.last_error = ""
                return True
            except Exception as e:
                self.last_error = str(e)[:200]
                self._log("[assist] chargement KO : %s" % e)
                return False
            finally:
                self.loading = False

    def unload(self):
        with self._lock:
            if self._model is None:
                return
            self._model = None
            self._tok = None
            try:
                import mlx.core as mx
                mx.clear_cache()
            except Exception:
                pass
            self._log("[assist] modèle déchargé (inactivité).")

    def unload_if_idle(self, idle_s=IDLE_UNLOAD_S):
        if self._model is not None and not self.loading and time.time() - self.last_use > idle_s:
            self.unload()

    # ---- génération bornée ----------------------------------------------
    def generate(self, system, user, max_tokens=600, timeout_s=8.0, repetition_penalty=None, exemples=None, progression=None):
        """Texte généré, ou None si délai dépassé / erreur. Greedy (température 0)."""
        if not self.load():
            return None
        result = {}

        def _run():
            try:
                with self._lock:
                    from mlx_lm.sample_utils import make_sampler
                    tok = self._tok
                    msgs = [{"role": "system", "content": system}]
                    for q, r in (exemples or []):
                        msgs += [{"role": "user", "content": q}, {"role": "assistant", "content": r}]
                    msgs.append({"role": "user", "content": user})
                    prompt = tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)
                    t = time.time()
                    kw = {}
                    if repetition_penalty:
                        from mlx_lm.sample_utils import make_logits_processors
                        kw["logits_processors"] = make_logits_processors(repetition_penalty=repetition_penalty, repetition_context_size=64)
                    # Toujours en flux : une génération dont le délai a expiré s'arrête vraiment
                    # (sinon elle garderait le verrou et ferait attendre la demande suivante).
                    from mlx_lm import stream_generate as _sgen
                    out, dernier = "", 0.0
                    for r in _sgen(self._model, tok, prompt=prompt, max_tokens=max_tokens,
                                   sampler=make_sampler(temp=0.0), **kw):
                        out += r.text
                        if progression is not None and time.time() - dernier > 0.25:
                            dernier = time.time()
                            try:
                                progression(len(out.split()))
                            except Exception:
                                pass
                        if result.get("stop"):
                            break
                    result["out"] = out
                    result["dt"] = time.time() - t
                    self.last_use = time.time()
            except Exception as e:
                result["err"] = str(e)[:200]

        th = threading.Thread(target=_run, name="assist-gen", daemon=True)
        th.start()
        th.join(timeout_s)
        if th.is_alive():
            result["stop"] = True
            self.last_error = "délai dépassé (%.0f s)" % timeout_s
            self._log("[assist] génération trop longue (> %.0f s) : brut conservé." % timeout_s)
            return None
        if "err" in result:
            self.last_error = result["err"]
            self._log("[assist] génération KO : %s" % result["err"])
            return None
        self.last_latency_s = result.get("dt")
        return result.get("out")


    def rediger(self, d, ctx=None, timeout_s=None):
        """Rédaction pour une demande reconnue par intents.detect. (texte, info) ; texte None => garder la dictée."""
        ctx = ctx or {}
        # Mettre en forme ce qui a été dicté n'a pas besoin d'un modèle : instantané, sans invention possible.
        det = rediger_sans_modele(d, ctx)
        if det:
            return det, "mise en forme directe"
        if d.get("source") == "presse-papiers" and not (ctx.get("contexte") or "").strip():
            return None, "rien de copié à retravailler"
        system, user, exemples, max_tokens = consigne_demande(d, ctx)
        budget = timeout_s or budget_redaction(max_tokens)
        out = self.generate(system, user, max_tokens=max_tokens, timeout_s=budget,
                            repetition_penalty=1.1 if d.get("mode") == "compose" else None, exemples=exemples,
                            progression=ctx.get("progression"))
        if out is None:
            return None, self.last_error or "sans réponse"
        ok, info, texte = accepte_demande(d, out, ctx)
        if not ok:
            self._log("[assist] résultat refusé (%s) : dictée conservée." % info)
            return None, info
        return signer(texte, d, ctx), info

    def extraire_rappel(self, texte):
        """{titre, quand} ou None. La date se calcule ensuite avec reminders.parse_datetime(quand)."""
        out = self.generate(RAPPEL_REGLE, texte, max_tokens=100, timeout_s=budget_redaction(100))
        return rappel_depuis_json(out) if out else None

    def compte_rendu(self, blocks, names=None, progress=None):
        """Compte rendu depuis les blocs [{speaker,text}] : extraction JSON par morceau,
        fusion déterministe, rendu Markdown. Retourne {"markdown", "noms", "morceaux", "morceaux_ok"}
        ou None si rien n'a pu être lu."""
        names = names or {}
        lignes = []
        for b in blocks or []:
            spk = b.get("speaker") or b.get("name") or b.get("locuteur") or "?"
            label = names.get(spk) or libelle_locuteur(spk)
            tx = (b.get("text") or b.get("texte") or "").strip()
            if tx:
                lignes.append("%s : %s" % (label, tx))
        if not lignes:
            return None
        morceaux, cur = [], ""
        for l in lignes:
            if cur and len(cur) + len(l) > CR_CHUNK_CHARS:
                morceaux.append(cur); cur = ""
            cur += l + "\n"
        if cur:
            morceaux.append(cur)
        extraits, ok = [], 0
        for i, m in enumerate(morceaux):
            out = self.generate(CR_CHUNK_REGLE, "Extrait %d/%d :\n%s" % (i + 1, len(morceaux), m), max_tokens=500, timeout_s=TIMEOUT_MEETING_S)
            d = extrait_depuis_json(out)
            if d is not None:
                extraits.append(d); ok += 1
            if progress:
                progress(i + 1, len(morceaux))
        if not ok:
            return None
        fusion = fusionner_extraits(extraits)
        return {"markdown": rendre_compte_rendu(fusion), "noms": fusion["noms"], "morceaux": len(morceaux), "morceaux_ok": ok}


def libelle_locuteur(spk):
    """spk_0 -> « Locuteur 1 » (numérotation humaine)."""
    m = re.search(r"(\d+)\s*$", str(spk or ""))
    return "Locuteur %d" % (int(m.group(1)) + 1) if m else str(spk)


def extrait_depuis_json(out):
    """Valide l'extraction d'un morceau : listes de chaînes, actions et noms en objets. None si illisible."""
    s = (out or "").strip()
    m = re.search(r"\{.*\}", s, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    def chaines(k):
        return [str(x).strip() for x in (d.get(k) or []) if isinstance(x, (str, int, float)) and str(x).strip()][:20]
    actions = []
    for a in (d.get("actions") or [])[:20]:
        if isinstance(a, dict) and str(a.get("quoi") or "").strip():
            actions.append({"qui": str(a.get("qui") or "").strip(), "quoi": str(a.get("quoi")).strip(), "quand": str(a.get("quand") or "").strip()})
        elif isinstance(a, str) and a.strip():
            actions.append({"qui": "", "quoi": a.strip(), "quand": ""})
    noms = []
    for n in (d.get("noms") or [])[:12]:
        if isinstance(n, dict):
            loc, pre = str(n.get("locuteur") or "").strip(), str(n.get("prenom") or "").strip()
            mm = re.search(r"(\d+)", loc)
            if mm and re.match(r"^[A-ZÀ-Ý][\w'’-]{1,30}$", pre):
                noms.append({"locuteur": int(mm.group(1)), "prenom": pre})
    return {"decisions": chaines("decisions"), "actions": actions, "sujets_ouverts": chaines("sujets_ouverts"), "noms": noms}


def fusionner_extraits(extraits):
    """Concatène les morceaux sans doublon ; les noms sont votés par locuteur."""
    vus = set()
    def uniq(items, cle):
        out = []
        for x in items:
            k = cle(x).lower()
            if k and k not in vus:
                vus.add(k); out.append(x)
        return out
    decisions = uniq([d for e in extraits for d in e["decisions"]], lambda x: x)
    actions = uniq([a for e in extraits for a in e["actions"]], lambda a: a["quoi"])
    sujets = uniq([s for e in extraits for s in e["sujets_ouverts"]], lambda x: x)
    votes = {}
    for e in extraits:
        for n in e["noms"]:
            votes.setdefault(n["locuteur"], {}).setdefault(n["prenom"], 0)
            votes[n["locuteur"]][n["prenom"]] += 1
    noms = {str(loc): max(c.items(), key=lambda kv: kv[1])[0] for loc, c in votes.items()}
    return {"decisions": decisions, "actions": actions, "sujets_ouverts": sujets, "noms": noms}


def rendre_compte_rendu(f):
    """Markdown final, écrit par nous : le modèle n'a fourni que des faits."""
    L = ["## Décisions"]
    L += ["- " + d for d in f["decisions"]] or ["Rien de noté."]
    L += ["", "## Actions"]
    L += ["- " + " : ".join(x for x in (a["qui"], a["quoi"]) if x) + (" (" + a["quand"] + ")" if a["quand"] else "") for a in f["actions"]] or ["Rien de noté."]
    L += ["", "## Sujets ouverts"]
    L += ["- " + s for s in f["sujets_ouverts"]] or ["Rien de noté."]
    if f["noms"]:
        L += ["", "## Noms entendus"]
        L += ["- Locuteur %s : %s" % (loc, nom) for loc, nom in sorted(f["noms"].items(), key=lambda kv: int(kv[0]))]
    return "\n".join(L)
