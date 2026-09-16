"""assist.py, l'intelligence locale de Vlocal (aperçu).

Un petit modèle de langage (LFM2.5-1.2B de Liquid AI, MLX, 4 bits, moins d'un
gigaoctet) tourne sur le Mac pour mettre une dictée en forme QUAND la personne
le demande (« c'est un mail pour Marie », « mets ça en liste », « en anglais »),
extraire un rappel d'une phrase en français ou en anglais, et rédiger le compte
rendu d'une réunion. Les règles, dans l'ordre :

1. Ce qui n'est pas demandé reste au mot près. Le nettoyage léger est une option
   séparée, et son résultat est refusé s'il s'éloigne trop du texte dicté.
2. Jamais sans borne : chaque appel a un délai maximal ; passé ce délai, ou à
   la moindre erreur, la dictée brute est insérée comme aujourd'hui.
3. Chargé à la demande, déchargé après inactivité : rien en mémoire tant que la
   fonction n'est pas utilisée.
4. Rien ne quitte le Mac. Le modèle est téléchargé une fois depuis R2, vérifié
   par empreinte, comme les modèles de transcription.

Ce module ne dépend d'aucun autre module de l'app : app.py le branche.
"""
import difflib
import json
import os
import re
import threading
import time

MODEL_NAME = "lfm2.5-1.2b-instruct-mlx-4bit"     # clé du manifeste R2 (section « optional »)
MODEL_LABEL = "LFM2.5-1.2B"
IDLE_UNLOAD_S = 10 * 60                          # déchargé après dix minutes sans usage
TIMEOUT_INTENT_S = 8.0                           # mise en forme demandée
TIMEOUT_CLEAN_S = 3.0                            # nettoyage léger : au-delà, brut
TIMEOUT_REMINDER_S = 4.0
TIMEOUT_COMMANDE_S = 12.0                        # une consigne exécutée (rédaction libre)
TIMEOUT_MEETING_S = 120.0

# --------------------------------------------------------------------------- #
# Intentions déclarées à la voix
# --------------------------------------------------------------------------- #

# nom dicté -> intention. Les variantes orthographiques que Whisper produit sont là.
_NOMS = {
    "mail": "mail", "e-mail": "mail", "email": "mail", "e mail": "mail", "courriel": "mail", "mél": "mail",
    "note": "note", "notes": "note",
    "liste": "liste", "liste à puces": "liste", "to-do": "liste", "todo": "liste", "to do": "liste",
    "prompt": "prompt", "prompte": "prompt", "prompt ia": "prompt",
    "message": "message", "texto": "message", "sms": "message", "message court": "message",
    "post": "post", "post linkedin": "post", "publication": "post", "publication linkedin": "post",
    "anglais": "en", "english": "en",
    "français": "fr", "francais": "fr", "french": "fr",
    "version courte": "court", "plus court": "court", "résumé": "court", "resume": "court",
    "formel": "formel", "ton formel": "formel", "version formelle": "formel", "soutenu": "formel",
    # anglais
    "list": "liste", "bullet list": "liste", "bullet points": "liste", "prompt for ai": "prompt",
    "text": "message", "text message": "message", "shorter": "court", "summary": "court", "formal": "formel",
    "formal tone": "formel", "linkedin post": "post",
}
# Marqueurs de queue qui se suffisent à eux-mêmes (« …, plus court. »)
_NOMS_QUEUE_LIBRES = {"plus court", "version courte", "version formelle", "ton formel", "shorter", "formal tone", "bullet points", "to-do", "todo", "to do", "liste à puces"}
_NOMS_ALT = "|".join(sorted((re.escape(k) for k in _NOMS), key=len, reverse=True))

# « c'est un mail pour Marie : … » / « this is an email to Paul: … » / « en anglais : … »
_TETE = re.compile(
    r"^\s*(?:alors|bon|ok|okay|donc)?[,\s]*"
    r"(?:(?:c'est|c est|ceci est|voici|voilà|voila|ça sera|ce sera|this is|it's|it is|here is|make this|"
    r"fais[- ]moi|fais|rédige[- ]moi|rédige|redige|écris[- ]moi|écris|ecris|prépare[- ]moi|prépare|tu me fais|tu me rédiges|tu rédiges|"
    r"je veux|je voudrais|j'aimerais|il me faut|mets[- ]moi ça en|mets ça en|mets[- ]moi en|"
    r"write me|write|draft me|draft|make me|give me|i want|i need|i'd like|turn this into|put this in)\s+(?:un|une|le|la|an?|the)?\s*)?"
    r"(?:(?:en|in|version|as)\s+)?"
    r"(?P<nom>" + _NOMS_ALT + r")"
    r"(?:\s+(?:pour|à|a|to|for)\s+(?P<dest>[^:,.\n]{1,40}?))?"
    r"\s*[:,]\s+(?P<contenu>.+)$", re.I | re.S)
# « … voilà, mets ça en mail. » / « … make it a list » / « … en anglais. »
_QUEUE = re.compile(
    r"^(?P<contenu>.+?)"
    r"[\s,.;:!]+(?:et\s+)?(?:voilà|voila|bon|donc|ok)?[,\s]*"
    r"(?:(?P<verbe>(?:mets|met|mets-moi|mets moi|met moi|transforme|passe|fais|formate|mets-le|mets le|tu me mets|tu mets|tu le mets|make|turn|put|format)"
    r"\s*(?:ça|sa|le|la|moi|moi ça|tout ça|le tout|tout|it|this|that|this into|it into)?\s*(?:en|dans|into|in|as|to)?\s*(?:un|une|an?|the)?\s*)"
    r"|(?P<prep>en\s+|in\s+|as\s+(?:an?\s+)?|version\s+|ton\s+))?(?P<nom>" + _NOMS_ALT + r")"
    r"(?:\s+(?:pour|à|a|to|for)\s+(?P<dest>[^:,.\n]{1,40}?))?"
    r"\s*[.!,]*\s*$", re.I | re.S)
# « réponds-lui que … » : la réponse s'appuie sur le texte copié
_REPONSE = re.compile(
    r"^\s*(?:alors|bon|ok)?[,\s]*(?:réponds|repond|répond|réponds-lui|réponds lui|réponds-leur|réponds leur|reply|answer)(?:\s*(?:-|\s)(?:lui|leur|him|her|them))?"
    r"\s*(?:que|qu'|:|,|that)?\s*(?P<contenu>.+)$", re.I | re.S)


# Une CONSIGNE adressée au modèle : un verbe d'action en tête (impératif ou demande
# polie) ET un livrable nommé dans les premiers mots. « Rédige un prompt pour créer une
# image d'un train poursuivi par un avion » -> le modèle rédige le prompt. Sans
# livrable nommé (« peux-tu m'envoyer le devis »), c'est du texte dicté, pas une consigne.
_CMD_VERBES = (r"rédige|redige|écris|ecris|génère|genere|crée|cree|propose|donne|trouve|fais|prépare|prepare|liste|traduis|résume|resume|"
               r"reformule|corrige|améliore|ameliore|explique|décris|decris|invente|imagine|suggère|suggere|formule|développe|developpe|"
               r"complète|complete|raccourcis|synthétise|synthetise|structure|produis|construis|dresse|élabore|elabore|"
               r"rédiges|rediges|écrives|ecrives|génères|generes|crées|crees|proposes|donnes|trouves|fasses|prépares|prepares|listes|traduises|"
               r"résumes|resumes|reformules|corriges|améliores|ameliores|expliques|décrives|decrives|inventes|suggères|suggeres|formules|"
               r"développes|developpes|complètes|completes|synthétises|synthetises|structures|produises|construises|dresses|élabores|elabores|"
               r"rédiger|rediger|écrire|ecrire|générer|generer|créer|creer|proposer|donner|trouver|faire|préparer|preparer|lister|traduire|"
               r"résumer|resumer|reformuler|corriger|améliorer|ameliorer|expliquer|décrire|decrire|inventer|imaginer|suggérer|suggerer|"
               r"formuler|développer|developper|compléter|completer|raccourcir|synthétiser|synthetiser|structurer|produire|construire|dresser|élaborer|elaborer|"
               r"write|draft|generate|create|give|find|make|prepare|list|translate|summarize|summarise|rewrite|fix|improve|explain|"
               r"describe|invent|imagine|suggest|come up with|develop|complete|shorten|structure|produce|build|compose")
_CMD_POLI = (r"(?:peux[- ]tu|pourrais[- ]tu|tu peux|est[- ]ce que tu (?:peux|pourrais)|j'aimerais que tu|je veux que tu|je voudrais que tu|"
             r"il faudrait que tu|tu (?:me|vas)|s'il te plaît|stp|"
             r"can you|could you|would you|please|i want you to|i need you to|i'd like you to)\s+(?:me\s+|m'|moi\s+)?")
_CMD_LIVRABLES = (r"prompts?|e-?mails?|mails?|courriels?|messages?|textos?|sms|textes?|réponses?|reponses?|résumés?|resumes?|synthèses?|syntheses?|"
                  r"descriptions?|listes?|idées?|idees?|titres?|noms?|slogans?|accroches?|plans?|paragraphes?|phrases?|introductions?|intros?|"
                  r"conclusions?|bios?|biographies?|posts?|publications?|tweets?|légendes?|legendes?|traductions?|versions?|brouillons?|notes?|"
                  r"exemples?|questions?|arguments?|synonymes?|définitions?|definitions?|explications?|poèmes?|poemes?|histoires?|discours|"
                  r"scripts?|comptes? rendus?|ordres? du jour|objets? de mail|formules?|reformulations?|recettes?|programmes?|planning|"
                  r"tableaux?|comparatifs?|pitch|argumentaires?|fiches?|checklists?|to-?do|variantes?|"
                  r"emails?|replies|reply|summary|summaries|list|lists|ideas?|titles?|names?|taglines?|hooks?|plans?|paragraphs?|sentences?|"
                  r"introduction|conclusion|bio|post|caption|captions|translation|version|draft|drafts|note|notes|examples?|questions?|"
                  r"arguments?|synonyms?|definitions?|explanation|poem|story|speech|script|report|agenda|subject line|checklist|pitch|table")
_COMMANDE = re.compile(
    r"^\s*(?:alors|bon|ok|okay|donc|hey|dis[- ]moi|dis)?[,\s]*"
    r"(?:" + _CMD_POLI + r")?"
    r"(?P<verbe>(?:" + _CMD_VERBES + r"))(?:[- ](?:moi|nous|me|le|la|les|lui|leur|y|en))?\s+"
    r"(?P<avant>(?:\w+[' ]){0,4}?)"                  # « un », « moi un », « cinq », « une bonne »
    r"(?P<objet>(?:" + _CMD_LIVRABLES + r"))\b"
    r"(?P<reste>.*)$", re.I | re.S)
# La consigne porte sur un texte déjà copié : « résume ça », « traduis ce texte », « corrige ce mail »
_CMD_SOURCE = re.compile(r"(?i)\b(ça|ca|cela|ce texte|ce mail|ce message|ce paragraphe|cet e-?mail|ce qui est copié|le texte copié|le message copié|"
                         r"le presse-papiers?|ci-dessus|this|it|that|the copied text|the clipboard|this text|this email|this message)\b")
# Verbes qui TRANSFORMENT un texte : sans texte inline ni presse-papier, pas de source, on ne fait rien.
_CMD_TRANSFORME = re.compile(r"(?i)^(traduis|résume|resume|reformule|corrige|améliore|ameliore|raccourcis|synthétise|synthetise|structure|complète|complete|"
                             r"translate|summarize|summarise|rewrite|fix|improve|shorten|complete)$")


_COMMANDE_SRC = re.compile(
    r"^\s*(?:alors|bon|ok|okay|donc)?[,\s]*(?:" + _CMD_POLI + r")?"
    r"(?P<verbe>" + _CMD_TRANSFORME.pattern.strip("^$(?i:)").lstrip("(").rstrip(")") + r"|traduire|résumer|resumer|reformuler|corriger|améliorer|ameliorer|raccourcir|synthétiser|synthetiser|structurer|compléter|completer)"
    r"(?:[- ](?:moi|me))?\s+(?P<src>ça|ca|cela|ce texte|ce mail|ce message|ce paragraphe|cet e-?mail|le texte copié|le message copié|le presse-papiers?|"
    r"this|it|that|this text|this email|this message|the copied text)\b(?P<reste>.*)$", re.I | re.S)
# explication d'un sujet : pas de livrable nommé mais un verbe qui n'a de sens que pour le modèle
_COMMANDE_EXPLIQUE = re.compile(
    r"^\s*(?:alors|bon|ok|okay|donc)?[,\s]*(?:" + _CMD_POLI + r")?"
    r"(?P<verbe>explique|expliquer|décris|decris|décrire|decrire|définis|definis|définir|definir|explain|describe|define)(?:[- ](?:moi|me))?\s+"
    r"(?!(?:lui|leur|à|a|au|aux|que|qu'|him|her|them|to)\b)(?P<reste>\S+(?:\s+\S+){2,}.*)$", re.I | re.S)
# Livrables qui supposent une matière première (on ne les fabrique pas à partir de rien)
_OBJETS_AVEC_SOURCE = re.compile(r"(?i)^(comptes? rendus?|résumés?|resumes?|synthèses?|syntheses?|traductions?|reformulations?|corrections?|summary|summaries|translation|report)$")
# Suite qui s'adresse à un humain : « … et envoie-le à Paul », « … puis préviens Julien »
_SUITE_HUMAINE = re.compile(r"(?i)\b(et|puis|ensuite)\s+(envoie|envoies|transmets|appelle|préviens|previens|passe|dis|rappelle|apporte|mets|range|send|call|tell|forward)\b")


def detect_commande(text):
    """(params, texte) si la dictée est une consigne pour le modèle, sinon None.
    params : objet (livrable), verbe, source ('clipboard' si la consigne vise un texte copié),
    transforme (le verbe attend un texte à retravailler)."""
    t = (text or "").strip()
    if len(t.split()) < 3 or len(t) > 1200:
        return None
    m = _COMMANDE.match(t) if len(t.split()) >= 4 else None
    if not m:
        ms = _COMMANDE_SRC.match(t)
        if ms:
            return {"objet": None, "verbe": ms.group("verbe").lower(), "source": "clipboard", "transforme": True}, t
        me = _COMMANDE_EXPLIQUE.match(t)
        if me and not _SUITE_HUMAINE.search(me.group("reste")):
            return {"objet": None, "verbe": me.group("verbe").lower(), "source": None, "transforme": False}, t
        return None
    verbe = m.group("verbe").lower()
    objet = m.group("objet").lower()
    reste = m.group("reste") or ""
    if _SUITE_HUMAINE.search(reste):
        return None
    # « donne le message à Paul », « fais la liste des courses demain » : un impératif adressé à
    # un HUMAIN a souvent un destinataire ; on ne le prend pas pour une consigne au modèle.
    if re.match(r"(?i)^\s*(à|a|au|aux|pour)\s+[A-ZÀ-Ý][\w'-]+\s*[.!]?\s*$", reste) and verbe in ("donne", "fais", "give", "make", "envoie"):
        return None
    # La source (texte copié) n'est retenue que si le démonstratif suit de près le verbe ou le
    # livrable (« résume ce texte en trois phrases »), pas un « ça » perdu plus loin.
    source = "clipboard" if _CMD_SOURCE.match(" ".join(reste.split()[:4])) else None
    if verbe in ("fais", "make", "donne", "give", "trouve", "find", "faire", "donner", "trouver") and not re.search(
            r"(?i)\b(un|une|des|deux|trois|quatre|cinq|six|dix|quelques|plusieurs|\d+|me|moi|a|an|some|two|three|four|five|ten|few|several)\b", m.group("avant") or ""):
        # « fais liste courses » / « donne message Paul » : trop court pour être une demande claire
        return None
    transforme = bool(_CMD_TRANSFORME.match(verbe)) or bool(_OBJETS_AVEC_SOURCE.match(objet))
    return {"objet": objet, "verbe": verbe, "source": source or ("clipboard" if transforme else None),
            "transforme": transforme}, t


def intention_pour_transformation(params, consigne, source):
    """Une consigne de transformation sur un texte copié devient l'intention spécialisée
    correspondante, appliquée à la source : « corrige ce mail » -> propre, « résume ça » -> court,
    « traduis ça en anglais » -> en, « reformule ça de manière formelle » -> formel."""
    verbe = (params.get("verbe") or "").lower()
    objet = (params.get("objet") or "").lower()
    c = (consigne or "").lower()
    if re.match(r"^(traduis|traduire|translate)", verbe) or re.match(r"^(traductions?|translation)$", objet):
        if re.search(r"\b(anglais|english)\b", c):
            return "en"
        if re.search(r"\b(français|francais|french)\b", c):
            return "fr"
        return "fr" if langue_probable(source) == "en" else "en"
    if re.match(r"^(résume|resume|résumer|resumer|synthétise|synthetise|synthétiser|synthetiser|raccourcis|raccourcir|summarize|summarise|shorten)", verbe) \
            or re.match(r"^(résumés?|resumes?|synthèses?|syntheses?|summary|summaries)$", objet):
        return "court"
    if re.search(r"\b(formel|formelle|soutenu|professionnel|professionnelle|formal|professional)\b", c):
        return "formel"
    if re.search(r"\b(liste|puces|list|bullet)\b", c):
        return "liste"
    if re.match(r"^(structure|structurer)", verbe) or re.search(r"\b(note)\b", c):
        return "note"
    if re.match(r"^(corrige|corriger|fix)", verbe) or re.match(r"^(corrections?)$", objet):
        return "corrige"
    # reformule, améliore, complète : on garde le propos et les mots (garde-fous du nettoyage)
    return "propre"


def detect_intent(text, lang="fr"):
    """Retourne (intention, paramètres, contenu sans le marqueur) ou (None, {}, text).

    Une intention n'est reconnue qu'au bord de la dictée, jamais au milieu, et
    seulement s'il reste du contenu à mettre en forme."""
    t = (text or "").strip()
    if not t:
        return None, {}, text
    m = _REPONSE.match(t)
    if m and len(m.group("contenu").split()) >= 2:
        return "reponse", {}, m.group("contenu").strip()
    m = _TETE.match(t)
    if m and len(m.group("contenu").split()) >= 2:
        nom = _NOMS[m.group("nom").lower()]
        params = {"dest": (m.group("dest") or "").strip()} if m.group("dest") else {}
        return nom, params, m.group("contenu").strip()
    m = _QUEUE.match(t)
    # Un nom nu en fin de phrase (« … la note », « … un objet de mail pour X ») n'est pas une
    # demande : il faut un verbe de mise en forme, une préposition, ou un marqueur qui se suffit.
    if m and len(m.group("contenu").split()) >= 3 and (m.group("verbe") or m.group("prep")
                                                       or m.group("nom").lower() in _NOMS_QUEUE_LIBRES):
        nom = _NOMS[m.group("nom").lower()]
        params = {"dest": (m.group("dest") or "").strip()} if m.group("dest") else {}
        return nom, params, m.group("contenu").strip().rstrip(",;:")
    c = detect_commande(t)
    if c:
        return "commande", c[0], c[1]
    return None, {}, text


# Le nettoyage par le modèle coûte une seconde et ne change presque jamais rien :
# Whisper ponctue déjà, processor retire les tics. On ne réveille le modèle que si
# le texte porte un défaut visible.
_DOUBLON = re.compile(r"(?i)\b(\w{2,})\s+\1\b")
_HESITATION = re.compile(r"(?i)\b(euh+|heu+|hum+|hmm+|bah|ben|hein)\b")


def besoin_nettoyage(texte):
    """True si un nettoyage a une chance d'apporter quelque chose."""
    t = (texte or "").strip()
    if not t:
        return False
    if _HESITATION.search(t) or _DOUBLON.search(t):
        return True
    mots = t.split()
    if t[0].islower() and len(mots) >= 3:
        return True
    if len(mots) >= 12 and not re.search(r"[.!?…]", t):
        return True
    if len(mots) >= 30 and len(re.findall(r"[.!?…,;:]", t)) < len(mots) / 25:
        return True
    return False


# --------------------------------------------------------------------------- #
# Consignes
# --------------------------------------------------------------------------- #

REGLE_COMMUNE = ("Tu mets en forme le texte dicté par la personne. Tu gardes son sens et ses informations, "
                 "tu n'ajoutes rien. Tu réponds par le texte final seulement.")

# La consigne vient APRÈS le texte : un modèle de cette taille recopie les
# interdits s'ils sont énoncés avant, et les traite comme du contenu.
CONSIGNES = {
    "corrige": "Corrige uniquement l'orthographe, la grammaire, les accords et la ponctuation de ce texte. Mêmes mots, même ordre, même sens ; ne reformule rien, n'ajoute rien.",
    "propre": "Réécris ce texte à l'identique, en corrigeant seulement la ponctuation, les majuscules, les hésitations (euh, hum) et les répétitions involontaires. Les nombres, dates et heures en chiffres. Même ordre, mêmes mots.",
    "mail": "Réécris ce texte sous forme de courriel{dest} : une ligne d'ouverture, le contenu en paragraphes courts, une formule de politesse{signature}. Garde les mots, les informations et le tutoiement ou vouvoiement du texte.",
    "note": "Réécris ce texte sous forme de note : un titre court sur la première ligne, puis une puce par idée, avec les mots du texte.",
    "liste": "Réécris ce texte sous forme de liste à puces : une action ou une idée par puce, avec les mots du texte. Rien d'autre que la liste.",
    "prompt": "Le texte ci-dessus est une demande que la personne veut adresser à une IA. Réécris cette demande en prompt structuré : « Contexte : », « Tâche : », « Contraintes : », « Format attendu : », chaque partie remplie avec ce que dit la demande, rien d'autre.",
    "message": "Réécris ce texte sous forme de message court{dest} : une ou deux phrases par idée, direct, sans longue formule de politesse. Garde les mots du texte.",
    "post": "Réécris ce texte comme une publication LinkedIn prête à publier, sans étiquette ni explication : première phrase courte, puis des paragraphes d'une à deux phrases séparés par une ligne vide. Garde les mots, les chiffres et les faits du texte, n'en ajoute pas.",
    "en": "Traduis ce texte en anglais naturel et professionnel. Réponds uniquement par la traduction.",
    "fr": "Traduis ce texte en français naturel et professionnel. Réponds uniquement par la traduction.",
    "court": "Raccourcis ce texte de moitié en gardant toutes les informations concrètes : noms, dates, chiffres, décisions.",
    "formel": "Réécris ce texte dans un registre formel et soutenu, avec exactement les mêmes informations.",
    "commande": "",   # traité à part : la demande EST le message, voir messages_pour
    "reponse": "La personne répond au message reçu ci-dessus. Rédige sa réponse à partir de son texte, dans la langue du message reçu, avec une ouverture et une formule de politesse courtes{signature}. Réponds uniquement par la réponse.",
}

ETIQUETTES = {"fr": {"propre": "Nettoyage", "mail": "Mail", "note": "Note", "liste": "Liste", "prompt": "Prompt", "message": "Message",
                     "post": "Post", "en": "Anglais", "fr": "Français", "court": "Plus court", "formel": "Formel", "reponse": "Réponse"},
              "en": {"propre": "Cleanup", "mail": "Email", "note": "Note", "liste": "List", "prompt": "Prompt", "message": "Message",
                     "post": "Post", "en": "English", "fr": "French", "court": "Shorter", "formel": "Formal", "reponse": "Reply"}}

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


REGLE_COMMANDE = ("Tu es un assistant d'écriture qui tourne sur l'ordinateur de la personne. Elle te dicte une demande : "
                  "tu y réponds directement, par le résultat demandé et rien d'autre : pas d'introduction, pas de commentaire, "
                  "pas de rappel de la demande, pas de question, pas de « voici ». Tu réponds dans la langue de la demande, "
                  "de façon concrète et précise, sans te répéter. Si un texte source est fourni, tu travailles uniquement "
                  "à partir de lui, sans inventer de fait, de nom ni de chiffre.\n"
                  "Exemples.\n"
                  "Demande : Donne-moi trois synonymes de lent.\nRéponse : paresseux, traînant, poussif\n"
                  "Demande : Écris un mail à Paul pour reporter le point de mardi à jeudi.\nRéponse : Bonjour Paul,\n\n"
                  "Je dois reporter notre point de mardi. Est-ce que jeudi, à la même heure, te conviendrait ?\n\nMerci et bonne journée,\n"
                  "Demande : Rédige un prompt pour créer une image d'un chat qui lit un journal.\nRéponse : Un chat roux assis dans un "
                  "fauteuil en cuir, lunettes sur le nez, lisant un journal déplié, lumière chaude du matin, style photo réaliste, 50 mm.")


def messages_pour(intent, contenu, ctx=None):
    """Construit (system, user) pour une intention. ctx : signature, dest, contexte, lang, params.
    Le texte vient d'abord, la consigne ensuite. Pour une commande, la demande EST le message."""
    ctx = ctx or {}
    if intent == "commande":
        user = ""
        src = (ctx.get("contexte") or "").strip()
        if src:
            user += "Texte source :\n" + src[:6000] + "\n\n"
        user += contenu.strip()
        lp = langue_probable(contenu)
        if lp == "en":
            user += "\n\n(Answer in English.)"
        elif lp == "fr":
            user += "\n\n(Réponds en français.)"
        return REGLE_COMMANDE, user
    consigne = CONSIGNES[intent]
    dest = ctx.get("dest") or ""
    signature = (ctx.get("signature") or "").strip()
    consigne = consigne.replace("{dest}", (" pour " + dest) if dest else "")
    consigne = consigne.replace("{signature}", (" et la signature « " + signature + " »") if signature else "")
    lang = ctx.get("lang") or "fr"
    if intent not in ("en", "fr") and lang == "en":
        consigne += " The text is in English: answer in English."
    user = ""
    if intent == "reponse":
        user += "Message reçu :\n" + (ctx.get("contexte") or "").strip()[:4000] + "\n\n"
    user += "Texte dicté :\n" + contenu.strip() + "\n\nConsigne : " + consigne
    return REGLE_COMMUNE, user


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
# Traces d'une consigne recopiée comme si c'était du contenu : le défaut typique
# d'un petit modèle. Aucune de ces expressions n'a sa place dans un texte utile.
_FUITES = re.compile(r"(?i)(texte dicté|la dictée|cette dictée|this dictation|dictation has been|reformul|"
                     r"n'ajout|ne pas ajouter|n'invent|ne pas inventer|sans commentaire|guillemets|langue du texte|"
                     r"assistant ia|format attendu :|cher\(e\)|interlocuteur\(e\)|objet : réponse à votre|maintains the original|"
                     r"garder le sens|modifier le sens|changer le sens|conserver (?:son|le) sens|sens initial|première (?:ligne|phrase) (?:courte|accroch)|"
                     r"texte source|résultat demandé|consigne)")
# Pour un prompt, « assistant IA » et « Format attendu : » sont le résultat demandé, pas une fuite.
_FUITES_PROMPT = re.compile(_FUITES.pattern.replace("assistant ia|format attendu :|", ""))
# Seuil de couverture (part des mots pleins retrouvés) par intention : une publication
# ou un courriel reformulent davantage qu'une liste.
_COUVERTURE_MIN = {"post": 0.45, "mail": 0.55, "reponse": 0.0}
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


_MOTS_EN = set("the and with for that this from your you are will can would should about into their there".split())
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


def commande_acceptable(consigne, out, source=None, params=None):
    """Une consigne exécutée : résultat différent de la consigne, dans sa langue, sur son
    sujet (la moitié des mots pleins de la demande s'y retrouvent, hors verbe et livrable),
    borné en longueur ; avec un texte source, au moins un tiers des mots pleins du résultat
    viennent de la source ou de la demande (anti-invention)."""
    ls = len(out)
    if ls > 3000:
        return False, "trop long"
    if similarite(consigne, out) > 0.85:
        return False, "consigne recopiée"
    if _META.match(out) or re.search(r"(?i)\b(je vais (?:t'|vous )?aider|n'hésitez pas|n'hésite pas|si tu veux|if you (?:want|need))\b", out[:200]):
        return False, "réponse méta"
    if repetitif(out):
        return False, "répétitif"
    if len(mots_pleins(out)) < 1:
        return False, "vide"
    params = params or {}
    lc, lo = langue_probable(consigne), langue_probable(out)
    traduction = bool(re.search(r"(?i)\b(anglais|english|français|french|traduis|translate|traduction|translation|in english|en anglais|en français)\b", consigne))
    if lc and lo and lc != lo and not traduction:
        return False, "langue différente (%s -> %s)" % (lc, lo)
    # Sujet : la demande nomme des choses (train, avion) que le résultat doit reprendre.
    # Sans objet pour une transformation d'un texte source, le sujet est la source elle-même.
    if not (source and params.get("transforme")):
        m = _COMMANDE.match(consigne.strip())
        sujet = (m.group("reste") or "") if m else consigne
        mots_sujet = [w for w in mots_pleins(sujet) if not re.match(r"(?i)^(" + _CMD_LIVRABLES + r")$", w)]
        if len(mots_sujet) >= 2:
            ms = set(mots_pleins(out))
            n = sum(1 for w in mots_sujet if w in ms or any(x.startswith(w[:5]) for x in ms))
            if n == 0 or n / len(mots_sujet) < 0.25:
                return False, "hors sujet (couverture %.2f)" % (n / len(mots_sujet))
    if source and not traduction:
        base = set(mots_pleins(source)) | set(mots_pleins(consigne))
        mo = mots_pleins(out)
        if mo:
            part = sum(1 for w in mo if w in base or any(x.startswith(w[:5]) for x in base)) / len(mo)
            if part < 0.34:
                return False, "s'éloigne de la source (%.2f)" % part
        verbe = (params.get("verbe") or "").lower()
        if re.match(r"(?i)^(corrige|corriger|reformule|reformuler|améliore|ameliore|améliorer|ameliorer|structure|structurer|complète|complete|compléter|fix|rewrite|improve)", verbe):
            # une correction ou une reformulation garde le propos : les mots pleins de la source
            # se retrouvent, la longueur reste comparable
            cv = couverture(source, out)
            if cv < 0.6 or len(out) < len(source) * 0.6 or len(out) > len(source) * 1.8 + 60:
                return False, "transformation infidèle (couverture %.2f)" % cv
        if re.match(r"(?i)^(résume|resume|résumer|resumer|synthétise|synthetise|raccourcis|raccourcir|summarize|summarise|shorten)", verbe) or \
                re.match(r"(?i)^(résumés?|resumes?|synthèses?|syntheses?|summary)$", (params.get("objet") or "")):
            if len(out) >= len(source):
                return False, "pas plus court que la source"
            mo = mots_pleins(out); ms = set(mots_pleins(source))
            if mo and sum(1 for w in mo if w in ms or any(x.startswith(w[:5]) for x in ms)) / len(mo) < 0.5:
                return False, "résumé qui s'éloigne de la source"
    if source and traduction:
        ls_, lo_ = len(source), len(out)
        if lo_ < ls_ * 0.45 or lo_ > ls_ * 2.2 + 40:
            return False, "longueur incohérente pour une traduction"
    return True, "ok"


def sortie_acceptable(intent, original, out, ctx_source=None, ctx_params=None):
    """Le résultat est-il digne d'être inséré à la place du brut ?

    Nettoyage : proche du texte (au moins 72 % des mots retrouvés dans l'ordre),
    pas plus long de 30 %, pas vide. Autres intentions : non vide, pas dix fois
    plus long, et le modèle n'a pas répondu à côté (pas de « je ne peux pas »)."""
    s = nettoyer_sortie(out)
    if not s:
        return False, "vide"
    lo, ls = len(original or ""), len(s)
    if re.search(r"(?i)^(je ne peux pas|i can(?:'|no)t|as an ai|en tant qu'ia)", s):
        return False, "refus"
    detecteur = _FUITES_PROMPT if intent == "prompt" else _FUITES
    fuite = detecteur.search(s)
    if fuite and not detecteur.search(original or ""):
        return False, "consigne recopiée (« %s »)" % fuite.group(0)
    if intent in ("propre", "corrige"):
        sim = similarite(original, s)
        if sim < (0.72 if intent == "propre" else 0.8):
            return False, "trop modifié (%.2f)" % sim
        if ls > lo * 1.3 + 20:
            return False, "rallongé"
        # Un nettoyage ne perd aucun mot plein : « Bonjour Marie, » avalé en tête
        # passait la similarité (0.82) tout en supprimant du contenu.
        cv = couverture(original, s)
        if cv < 0.9:
            return False, "mots perdus (couverture %.2f)" % cv
        return True, "ok %.2f" % sim
    if intent in ("en", "fr"):
        # autre langue : on ne peut pas compter les mots, mais une traduction fait à peu près la même longueur
        if ls < lo * 0.45 or ls > lo * 2.2 + 40:
            return False, "longueur incohérente pour une traduction"
        return True, "ok"
    if intent == "commande":
        return commande_acceptable(original, s, ctx_source, ctx_params)
    if intent == "court":
        if ls > lo:
            return False, "pas plus court"
        cv = couverture(original, s)
        return (cv >= 0.35, "couverture %.2f" % cv)
    if ls > lo * 6 + 400:
        return False, "trop long"
    cv = couverture(original, s)
    if cv < _COUVERTURE_MIN.get(intent, 0.6):
        return False, "contenu perdu (couverture %.2f)" % cv
    return True, "ok, couverture %.2f" % cv


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
            if not titre:
                return None
            return {"titre": titre[0].upper() + titre[1:], "when": r[0], "label": r[1], "confidence": r[2]}
    return None


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
    def generate(self, system, user, max_tokens=600, timeout_s=8.0, repetition_penalty=None):
        """Texte généré, ou None si délai dépassé / erreur. Greedy (température 0)."""
        if not self.load():
            return None
        result = {}

        def _run():
            try:
                with self._lock:
                    from mlx_lm import generate as _gen
                    from mlx_lm.sample_utils import make_sampler
                    tok = self._tok
                    prompt = tok.apply_chat_template(
                        [{"role": "system", "content": system}, {"role": "user", "content": user}],
                        tokenize=False, add_generation_prompt=True)
                    t = time.time()
                    kw = {}
                    if repetition_penalty:
                        from mlx_lm.sample_utils import make_logits_processors
                        kw["logits_processors"] = make_logits_processors(repetition_penalty=repetition_penalty, repetition_context_size=64)
                    out = _gen(self._model, tok, prompt=prompt, max_tokens=max_tokens,
                               sampler=make_sampler(temp=0.0), verbose=False, **kw)
                    result["out"] = out
                    result["dt"] = time.time() - t
                    self.last_use = time.time()
            except Exception as e:
                result["err"] = str(e)[:200]

        th = threading.Thread(target=_run, name="assist-gen", daemon=True)
        th.start()
        th.join(timeout_s)
        if th.is_alive():
            self.last_error = "délai dépassé (%.0f s)" % timeout_s
            self._log("[assist] génération trop longue (> %.0f s) : brut conservé." % timeout_s)
            return None
        if "err" in result:
            self.last_error = result["err"]
            self._log("[assist] génération KO : %s" % result["err"])
            return None
        self.last_latency_s = result.get("dt")
        return result.get("out")

    # ---- usages -----------------------------------------------------------
    def transformer(self, intent, contenu, ctx=None, timeout_s=None):
        """Met en forme selon l'intention. (texte, info) ; texte None => garder le brut."""
        if intent not in CONSIGNES:
            return None, "intention inconnue"
        system, user = messages_pour(intent, contenu, ctx)
        budget = timeout_s or (TIMEOUT_CLEAN_S if intent == "propre" else TIMEOUT_INTENT_S)
        n_mots = len(contenu.split())
        max_tokens = min(1200, max(80, int(n_mots * 3.0) + 60))
        if intent == "commande":
            params = (ctx or {}).get("params") or {}
            source = ((ctx or {}).get("contexte") or "").strip()
            if params.get("transforme"):
                if not source:
                    return None, "consigne de transformation sans texte source"
                cible = intention_pour_transformation(params, contenu, source)
                if cible == "corrige":
                    # Mesuré sur le vrai modèle : la correction orthographique change le sens
                    # une fois sur deux (« on peut » -> « sur peu »). Tant que ce n'est pas
                    # fiable, une demande de correction laisse le texte tel quel.
                    return None, "correction désactivée sur ce modèle (change le sens)"
                out, info = self.transformer(cible, source, dict(ctx or {}, params={}, contexte=""), timeout_s)
                return out, "%s -> %s : %s" % ("commande", cible, info)
            max_tokens = 700
            budget = timeout_s or TIMEOUT_COMMANDE_S
        out = self.generate(system, user, max_tokens=max_tokens, timeout_s=budget,
                            repetition_penalty=1.15 if intent == "commande" else None)
        if out is None:
            return None, self.last_error or "sans réponse"
        if intent == "commande":
            out = nettoyer_commande(out)
        ok, info = sortie_acceptable(intent, contenu, out, (ctx or {}).get("contexte"), (ctx or {}).get("params"))
        if not ok:
            self._log("[assist] résultat refusé (%s) : brut conservé." % info)
            return None, info
        texte = nettoyer_sortie(out)
        if intent == "commande":
            signature = ((ctx or {}).get("signature") or "").strip()
            objet = (((ctx or {}).get("params") or {}).get("objet") or "").lower()
            if signature and re.match(r"(?i)^(e-?mails?|mails?|courriels?|réponses?|reponses?|replies|reply)$", objet) \
                    and signature.lower() not in texte[-80:].lower():
                texte = texte.rstrip() + "\n\n" + signature
        return texte, info

    def extraire_rappel(self, texte):
        """{titre, quand} ou None. La date se calcule ensuite avec reminders.parse_datetime(quand)."""
        out = self.generate(RAPPEL_REGLE, texte, max_tokens=100, timeout_s=TIMEOUT_REMINDER_S)
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
