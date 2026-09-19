"""Vlocal 1.4.2 : reconnaissance des demandes de mise en forme dictées.

Un seul travail : dire, sans modèle et en moins d'une milliseconde, si une dictée
porte une demande (« c'est un mail pour Marie : … », « fais-moi un prompt pour… »,
« …, mets ça en liste ») et laquelle. Sans demande reconnue, la dictée passe
telle quelle, instantanément : le modèle n'est jamais réveillé.

Deux façons de demander, deux modes :
  - FORMAT  : la dictée EST le texte, on le met en forme (« c'est un mail pour
              Marie : merci pour ton retour… », « …, mets ça en liste »).
  - COMPOSE : la dictée DÉCRIT ce qu'il faut écrire (« rédige-moi un mail pour
              décaler la réunion de jeudi », « fais un prompt pour générer une
              image d'un train »).

Une demande n'est reconnue qu'au bord de la dictée : dans ses premiers mots, ou
comme dernière proposition détachée par une ponctuation. Jamais au milieu d'une
phrase (« je t'envoie ça en mail demain » n'est pas une demande).

Le texte est comparé sans accents ni majuscules ; les positions restent celles
du texte d'origine, dont on extrait le contenu tel qu'il a été dicté.
"""
import re
import unicodedata

# --------------------------------------------------------------------------- #
# Normalisation (même longueur que l'original : les positions se correspondent)
# --------------------------------------------------------------------------- #
_APOS = {"’": "'", "‘": "'", "`": "'", "´": "'", "«": '"', "»": '"', "“": '"', "”": '"', "–": "-", "—": "-", " ": " ", " ": " "}


def normaliser(texte):
    out = []
    for ch in texte or "":
        ch = _APOS.get(ch, ch)
        if ch in "œŒ":
            out.append("o")
            continue
        if ch in "æÆ":
            out.append("a")
            continue
        base = unicodedata.normalize("NFD", ch)[0] if ch.isalpha() else ch
        out.append(base.lower() if len(base.lower()) == 1 else base)
    return "".join(out)


# --------------------------------------------------------------------------- #
# Lexique (formes normalisées : sans accents, minuscules)
# --------------------------------------------------------------------------- #
# livrable -> variantes (y compris ce que Whisper écrit : « prompte », « e mail », « mél »)
LIVRABLES = {
    "mail": [r"e[- ]?mails?", r"mails?", r"mels?", r"courriels?", r"emails?"],
    "prompt": [r"promp?t(?:e|es|s)?", r"promts?"],
    "message": [r"messages?", r"textos?", r"sms", r"dm", r"text messages?", r"texts?"],
    "note": [r"notes?", r"memos?", r"comptes?[- ]rendus?", r"prises? de notes?"],
    "liste": [r"listes? a puces", r"listes?", r"to[- ]?do(?: lists?)?", r"checklists?", r"check[- ]lists?", r"bullet points?", r"bullet lists?", r"lists?"],
    "post": [r"posts? linkedin", r"posts?", r"publications? linkedin", r"publications?", r"tweets?", r"threads?", r"linkedin posts?"],
    "reponse": [r"reponses?", r"replies", r"reply"],
}
_LIV_ALT = "|".join(sorted((v for vs in LIVRABLES.values() for v in vs), key=len, reverse=True))
_LIV_RE = [(k, re.compile(r"^(?:%s)$" % "|".join(vs))) for k, vs in LIVRABLES.items()]


def livrable_de(mot):
    m = (mot or "").strip().lower()
    for k, rx in _LIV_RE:
        if rx.match(m):
            return k
    return None


# Qualificatifs qui suivent le livrable et le précisent (« mail de relance », « prompt pour Midjourney »)
_QUALIF = (r"(?:\s+(?:linkedin|ia|ai|chatgpt|claude|midjourney|slack|whatsapp|teams|pro|professionnel(?:le)?|formel(?:le)?|"
           r"court|courte|rapide|de (?:relance|prospection|reponse|remerciement|confirmation|suivi|bienvenue|presentation|motivation|candidature|"
           r"rappel|felicitations|excuse|excuses|refus|invitation|recadrage|synthese|la reunion)|d'(?:image|excuse|excuses|invitation|information)|"
           r"de \w+ (?:image|video)|image|video|systeme|system))?")
# Déterminants et adjectifs tolérés entre le verbe et le livrable
_DET = (r"(?:(?:un|une|le|la|l'|les|des|du|de|d'|ce|cet|cette|ces|mon|ma|mes|ton|ta|notre|votre|a|an|the|this|my|some|quelques)\s*)?"
        r"(?:(?:petit|petite|court|courte|bref|breve|joli|jolie|beau|bel|belle|bon|bonne|vrai|vraie|nouveau|nouvel|nouvelle|premier|premiere|"
        r"super|tres|vraiment|parfait|parfaite|superbe|meilleur|meilleure|short|quick|nice|good|great|proper|formal|formel|formelle|"
        r"professionnel|professionnelle|pro|clean|propre|structure|structuree|detaille|detaillee|complet|complete)\s+){0,2}")
# « sous forme de », « au format », « en », « comme »
_FORME = r"(?:(?:sous (?:la )?forme d(?:e |'|'un |'une |un |une )|au format |en format |format |en mode |en |comme |style |facon |type |into (?:an? |the )?|as (?:an? |the )?|in (?:an? |the )?)\s*)"

# Petits mots d'amorce ignorés en tête
_AMORCE = r"^\s*(?:(?:alors|bon|ok|okay|donc|euh+|heu+|du coup|hey|tiens|voila|voici|ben|bah|allez|so|well|right|hmm+|hum+|oui|yes|ah|eh|ecoute|ecoutez)\s*[,.!:]?\s*){0,3}"

# Verbes qui CRÉENT un livrable (« rédige un mail », « fais-moi un prompt »)
_V_CREE = (r"fais|fait|faire|faites|redige|rediges|rediger|redigez|ecris|ecrit|ecrire|ecrivez|ecrives|etablis|etablit|etablir|prepare|prepares|preparer|preparez|"
           r"genere|generes|generer|cree|crees|creer|creez|formule|formules|formuler|construis|construire|compose|composes|composer|elabore|elaborer|"
           r"dresse|dresser|produis|produire|propose|proposes|proposer|donne|donnes|donner|sors|sortir|pond|ponds|pondre|fasses|redigiez|"
           r"write|draft|create|generate|make|prepare|compose|give|produce|build|put together|come up with")
# Verbes qui CONVERTISSENT le texte dicté (« mets ça en liste », « transforme ça en prompt »)
_V_CONVERTIT = (r"mets|met|mettre|mettez|transforme|transformes|transformer|transformez|passe|passes|passer|convertis|convertir|tourne|tourner|formate|formater|"
                r"reformule|reformuler|restructure|structure|structurer|arrange|organise|organiser|turn|put|convert|format|rewrite|reformat|make")
_PRON = r"(?:[- ](?:moi|nous|le|la|les|en|lui|leur|me|m'|y))?(?:[- ](?:moi|le|la|en))?"
_CA = r"(?:(?:ca|cela|tout ca|tout cela|le tout|ce texte|ce qui precede|ce que je (?:viens de dire|dis|dicte)|it|this|that|all this|all of this|everything)\s+)"

# Demandes polies : « peux-tu me rédiger… », « j'aimerais que tu fasses… »
_POLI = (r"(?:est[- ]ce que tu (?:peux|pourrais|veux bien|voudrais bien)|est[- ]ce que vous (?:pouvez|pourriez)|peux[- ]tu|pourrais[- ]tu|pouvez[- ]vous|pourriez[- ]vous|"
         r"tu peux|tu pourrais|tu veux bien|vous pouvez|vous pourriez|tu me|vous me|merci de|je veux que tu|je voudrais que tu|j'aimerais que tu|"
         r"je souhaite que tu|je souhaiterais que tu|il faudrait que tu|il faut que tu|j'ai besoin que tu|"
         r"can you|could you|would you|will you|please|pls|i want you to|i need you to|i'd like you to|i would like you to)\s+(?:me |m'|nous |moi |svp |s'il te plait )?")
# Objectif : « l'objectif c'est un mail », « j'ai besoin d'un prompt »
_OBJECTIF = (r"(?:l'?objectif(?:,)?\s*(?:c'?est|est|:)|le but(?:,)?\s*(?:c'?est|est|:)|l'?idee(?:,)?\s*(?:c'?est|est)|ce que je (?:veux|voudrais|souhaite|cherche)(?:,)?\s*c'?est|"
             r"ce qu'?il (?:me )?faut(?:,)?\s*c'?est|j'?ai besoin d'?|il me faut|il faudrait|il faut|je (?:veux|voudrais|souhaite|souhaiterais|cherche|cherche a avoir)|j'?aimerais|"
             r"on (?:veut|voudrait)|i (?:want|need|would like|'d like)|the goal is|the aim is|what i (?:want|need) is)\s*"
             r"(?:(?:faire|avoir|ecrire|rediger|obtenir|creer|preparer|to write|to have|to make|to get|to draft)\s+)?")
# Déclaratif : « c'est un prompt », « ceci est un mail », « voici un message »
_DECLARE = (r"(?:c'?est|c est|ceci est|ca c'?est|ca,? c'?est|voici|voila|ca va etre|ca sera|ce sera|c'?est pour|il s'agit d'?|on part sur|on est sur|"
            r"la c'?est|la,? c'?est|this is|here'?s|here is|it'?s|it is|that'?s)\s*")

_DEST = (r"(?:\s+(?:pour|a|au|aux|to|for|destine a|a l'attention d[e']|adresse a|en reponse a)\s+"
         r"(?P<dest>(?:(?:l'|le |la |les |mon |ma |mes |notre |nos |votre |vos |ton |ta |tes |my |our |the )?"
         r"(?:equipe|client|cliente|clients|boss|manager|patron|direction|collegue|collegues|prestataire|fournisseur|recruteur|candidat|prospect|team|customer|colleague)"
         r"(?: [a-z]+)?)|(?:[A-Za-z][\w'-]+(?: [A-Za-z][\w'-]+){0,2})))?")

# Ce qui, après un livrable en tête, annonce une DESCRIPTION (mode COMPOSE) plutôt qu'un corps
_INTRO_DESCRIPTION = re.compile(
    r"^\s*(?:pour (?:lui |leur |te |vous |me |nous )?(?:\w+er|\w+ir|\w+re|dire|faire|annoncer|demander|confirmer|expliquer|proposer|remercier|relancer|"
    r"presenter|inviter|prevenir|informer|decaler|reporter|annuler|repondre|rappeler)\b|pour qu|afin d|qui |que |qu'|sur |a propos d|au sujet d|concernant |"
    r"de (?:relance|prospection|reponse|remerciement|confirmation|suivi|bienvenue|presentation)|about |to (?:\w+)\b|for (?:a|an|the|my)\b|that |explaining |asking |"
    r"telling |avec |where |dans lequel |dans laquelle |ou |en expliquant |en disant |en demandant |en proposant |en annoncant )", re.I)

# Modificateurs : langue, plus court, plus formel
_LANGUES = {"anglais": "en", "english": "en", "francais": "fr", "french": "fr", "espagnol": "es", "spanish": "es",
            "allemand": "de", "german": "de", "italien": "it", "italian": "it"}
_LANG_ALT = "|".join(_LANGUES)
_MOD_LANGUE = re.compile(r"(?:(?:traduis|traduire|traduit|translate)(?:[- ](?:le|la|les|moi|ca|it|this))*\s+)?(?:en|in|version|into|to)\s+(?P<langue>%s)" % _LANG_ALT)
_MOD_COURT = re.compile(r"(?:en\s+)?(?:plus court|plus courte|plus concis|plus concise|version courte|version plus courte|en bref|en resume|resume[- ]le|resume[- ]la|resume ca|"
                        r"raccourcis(?:[- ](?:le|la|ca))?|synthetise(?:[- ](?:le|la|ca))?|shorter|make it shorter|more concise)")
_MOD_FORMEL = re.compile(r"(?:en\s+)?(?:plus formel|plus formelle|plus pro|plus professionnel|plus professionnelle|ton formel|version formelle|en vouvoyant|vouvoie|"
                         r"de maniere (?:plus )?formelle|de facon (?:plus )?formelle|more formal|formal tone|more professional)")


class Demande(dict):
    """kind, livrable, mode ('format'|'compose'), dest, qualif, langue, court, formel,
    contenu (texte d'origine sans le marqueur), marqueur (texte du marqueur), ou ('tete'|'queue')."""
    __getattr__ = dict.get


_AVEC_CA = r"(?:\s+(?:avec|de|a partir de|from|out of|with)\s+(?:ca|cela|tout ca|tout cela|ce texte|it|this|that))?"
_SEP_TETE = _AVEC_CA + r"\s*(?P<sep>[:,.!;?\-]|\s)\s*"


def _mk(pattern):
    return re.compile(pattern, re.S)


# Tête : famille, motif. Le livrable est capturé dans (?P<liv>…).
_TETES = [
    ("declare", _mk(_AMORCE + _DECLARE + _DET + r"(?P<liv>" + _LIV_ALT + r")\b" + _QUALIF + _DEST + _SEP_TETE)),
    ("objectif", _mk(_AMORCE + _OBJECTIF + _DET + r"(?P<liv>" + _LIV_ALT + r")\b" + _QUALIF + _DEST + _SEP_TETE)),
    ("poli", _mk(_AMORCE + _POLI + r"(?:" + _V_CREE + r")" + _PRON + r"\s+(?:" + _CA + r"(?:en |sous forme d'?|au format |comme )?)?" + _DET
                 + r"(?P<liv>" + _LIV_ALT + r")\b" + _QUALIF + _DEST + _SEP_TETE)),
    ("cree", _mk(_AMORCE + r"(?:" + _V_CREE + r")" + _PRON + r"\s+" + _DET + r"(?P<liv>" + _LIV_ALT + r")\b" + _QUALIF + _DEST + _SEP_TETE)),
    ("convertit", _mk(_AMORCE + r"(?:" + _V_CONVERTIT + r")" + _PRON + r"\s+" + _CA + r"?" + _FORME + _DET + r"(?P<liv>" + _LIV_ALT + r")\b" + _QUALIF + _DEST + _SEP_TETE)),
    ("forme", _mk(_AMORCE + r"(?:sous (?:la )?forme d(?:e |'|'un |'une |un |une )|au format |en format |format |en mode |en |as (?:an? )?|in (?:an? )?)\s*" + _DET
                  + r"(?P<liv>" + _LIV_ALT + r")\b" + _QUALIF + _DEST + r"\s*(?P<sep>:)\s*")),
]

# Queue : la demande est la dernière proposition, détachée par une ponctuation (ou « et », « voilà »)
_Q_DEBUT = r"(?:^|[.,;:!?]\s*|\s(?:et|voila|donc|bref)\s*[,:]?\s*)(?:(?:voila|donc|ok|bon|et|bref|alors)\s*[,:]?\s*){0,2}"
_Q_FIN = r"\s*(?:s'il te plait|s'il vous plait|stp|svp|please|merci)?\s*[.!?]*\s*$"
_QUEUES = [
    ("declare", _mk(_Q_DEBUT + r"(?:c'?est|ca c'?est|ca sera|ce sera|c'?etait|c'?est pour|ca,? c'?est|this is|it'?s|it is|that'?s)\s*" + _DET
                    + r"(?P<liv>" + _LIV_ALT + r")\b" + _QUALIF + _DEST + _Q_FIN)),
    ("convertit", _mk(_Q_DEBUT + r"(?:" + _POLI + r")?(?:" + _V_CONVERTIT + r"|" + _V_CREE + r")" + _PRON + r"\s+(?:" + _CA + r")?" + _FORME + r"?" + _DET
                      + r"(?P<liv>" + _LIV_ALT + r")\b" + _QUALIF + _DEST + r"(?:\s+(?:avec|de|a partir de|from|out of|with)\s+(?:ca|cela|tout ca|tout cela|it|this|that))?" + _Q_FIN)),
    ("forme", _mk(_Q_DEBUT + r"(?:sous (?:la )?forme d(?:e |'|'un |'une )|au format |en format |format |en mode |as (?:an? )?|in (?:an? )?|en )\s*" + _DET
                  + r"(?P<liv>" + _LIV_ALT + r")\b" + _QUALIF + _DEST + _Q_FIN)),
]
_Q_MODS = _mk(_Q_DEBUT + r"(?:(?:" + _POLI + r")?(?:mets|met|mettre|passe|transforme|ecris|redige|fais)(?:[- ](?:le|la|moi|ca))?\s+(?:" + _CA + r")?)?(?P<mod>"
              + _MOD_LANGUE.pattern + r"|" + _MOD_COURT.pattern + r"|" + _MOD_FORMEL.pattern + r")" + _Q_FIN)
_T_MODS = _mk(_AMORCE + r"(?:(?:" + _POLI + r")?(?:mets|met|mettre|passe|transforme|ecris|redige|fais)(?:[- ](?:le|la|moi|ca))?\s+(?:" + _CA + r")?)?(?P<mod>"
              + _MOD_LANGUE.pattern + r"|" + _MOD_COURT.pattern + r"|" + _MOD_FORMEL.pattern + r")\s*[:,.]\s+")

# Réponse à un texte copié
_REPONSE = _mk(_AMORCE + r"(?!reponds[- ]moi|repond[- ]moi|reponds[- ]nous)(?:reponds|repond|reponds[- ](?:lui|leur|y)|reponds a (?:ce|cet|cette) (?:mail|message|e-?mail|texte)|fais[- ](?:lui|leur) une reponse|"
               r"redige (?:la|une) reponse|reply|answer|respond)(?:\s*(?:que|qu'|:|,|that|to (?:this|it|him|her|them)))?\s*(?P<corps>.+)$")

# Consignes de transformation d'un texte copié (sans livrable) : « traduis ça en anglais », « résume ce texte »
_SOURCE = r"(?:ca|cela|ce texte|ce mail|ce message|ce paragraphe|cet e-?mail|le texte copie|le message copie|le presse-papiers?|ce que j'ai copie|this|it|that|this text|this email|this message|the copied text)"
_TRANSFO = _mk(_AMORCE + r"(?:" + _POLI + r")?(?P<verbe>traduis|traduire|traduises|resume|resumer|resumes|reformule|reformuler|reformules|raccourcis|raccourcir|synthetise|synthetiser|"
               r"ameliore|ameliorer|ameliores|translate|summarize|summarise|rewrite|shorten|improve)(?:[- ](?:moi|le|la|les))?\s+" + _SOURCE + r"\b(?P<reste>.*)$")

# Commandes générales au modèle (sans livrable nommé ci-dessus) : « donne-moi trois idées de… », « explique… »
_CMD_OBJETS = (r"idees?|titres?|noms?|slogans?|accroches?|plans?|paragraphes?|phrases?|introductions?|intros?|conclusions?|bios?|biographies?|legendes?|"
               r"descriptions?|exemples?|questions?|arguments?|synonymes?|definitions?|explications?|poemes?|histoires?|discours|scripts?|"
               r"objets? de mail|objets? d'e-?mail|formules?|recettes?|programmes?|tableaux?|comparatifs?|pitch|argumentaires?|fiches?|variantes?|resumes?|syntheses?|"
               r"ideas?|titles?|names?|taglines?|hooks?|paragraphs?|sentences?|introduction|conclusion|bio|captions?|examples?|arguments?|synonyms?|definitions?|"
               r"explanation|poem|story|speech|script|subject lines?|pitch|table|description|summary|outline")
_CMD = _mk(_AMORCE + r"(?:" + _POLI + r")?(?P<verbe>" + _V_CREE + r"|trouve|trouver|trouves|liste|lister|imagine|imaginer|invente|inventer|suggere|suggerer|find|list|suggest|imagine|invent)"
           + _PRON + r"\s+(?P<avant>(?:[\w']+\s+){0,3}?)(?P<objet>" + _CMD_OBJETS + r")\b(?P<reste>.*)$")
_CMD_EXPLIQUE = _mk(_AMORCE + r"(?:" + _POLI + r")?(?P<verbe>explique|expliquer|expliques|decris|decrire|definis|definir|explain|describe|define)(?:[- ](?:moi|me|nous))?\s+"
                    r"(?!(?:lui|leur|a |au |aux |que |qu'|him |her |them |to ))(?P<reste>\S+(?:\s+\S+){2,}.*)$")
# Suite adressée à une personne : « … et envoie-le à Paul » -> ce n'est pas pour le modèle
_SUITE_HUMAINE = re.compile(r"\b(?:et|puis|ensuite|and|then)\s+(?:envoie|envoies|envoyer|transmets|transmettre|appelle|previens|passe|dis|rappelle|apporte|range|imprime|"
                            r"poste|publie|partage|send|call|tell|forward|post|share|print)\b")
# Parler DU livrable n'est pas le demander : « le mail que j'ai reçu », « ce prompt ne marche pas »
_AU_SUJET_DE = re.compile(r"^\s*(?:que|qu'|qui|dont|n'|ne |est |etait |a ete |marche|fonctionne|ne marche|that |which |is |was |doesn't|didn't|isn't)")

# Applications où ce qu'on dicte s'adresse à une autre IA ou à une machine : une consigne
# (« rédige-moi un mail pour… ») y est pour elle, pas pour nous. Un format DÉCLARÉ
# (« c'est un prompt : … ») reste appliqué partout : c'est une demande explicite.
APPS_ASSISTANTS = {
    "com.anthropic.claudefordesktop", "com.openai.chat", "com.openai.codex", "ai.perplexity.mac", "com.google.GeminiMacOS",
    "com.apple.Terminal", "com.googlecode.iterm2", "dev.warp.Warp-Stable", "com.mitchellh.ghostty", "net.kovidgoyal.kitty",
    "io.alacritty", "com.todesktop.230313mzl4w4u92", "com.microsoft.VSCode", "com.microsoft.VSCodeInsiders", "com.exafunction.windsurf",
    "com.jetbrains.intellij", "com.jetbrains.pycharm", "dev.zed.Zed", "com.raycast.macos", "co.anysphere.cursor",
    "com.apple.dt.Xcode", "ai.opencode.desktop", "com.nousresearch.hermes.setup",
}
_PREFIXES_ASSISTANTS = ("com.anthropic.", "com.openai.", "com.jetbrains.", "com.nousresearch.", "ai.opencode.", "com.microsoft.VSCode")
_TITRES_ASSISTANTS = re.compile(r"(?i)\b(chatgpt|claude|perplexity|gemini|mistral|le chat|copilot|deepseek|grok|poe|hugging ?chat|lm studio)\b")


def contexte_assistant(bundle_id=None, titre=None):
    """True si l'application au premier plan est un assistant IA, un terminal ou un éditeur de code."""
    if bundle_id and (bundle_id in APPS_ASSISTANTS or bundle_id.startswith(_PREFIXES_ASSISTANTS)):
        return True
    if titre and _TITRES_ASSISTANTS.search(titre):
        return True
    return False


# --------------------------------------------------------------------------- #
# Détection
# --------------------------------------------------------------------------- #
def _nb_mots(s):
    return len(re.findall(r"\w+", s or ""))


def _mods(n_mod):
    d = {}
    m = _MOD_LANGUE.search(n_mod or "")
    if m:
        d["langue"] = _LANGUES[m.group("langue")]
    if _MOD_COURT.search(n_mod or ""):
        d["court"] = True
    if _MOD_FORMEL.search(n_mod or ""):
        d["formel"] = True
    return d


def _dest(orig, m):
    """Destinataire, lu dans le texte d'origine (on garde la casse) ; un nom propre doit
    commencer par une majuscule, un rôle (« l'équipe ») peut être en minuscules."""
    if "dest" not in m.re.groupindex or not m.group("dest"):
        return "", None
    a, b = m.span("dest")
    brut = orig[a:b].strip()
    n = m.group("dest")
    if re.match(r"(?:l'|le |la |les |mon |ma |mes |notre |nos |votre |vos |ton |ta |tes |my |our |the )?"
                r"(?:equipe|client|cliente|clients|boss|manager|patron|direction|collegue|collegues|prestataire|fournisseur|recruteur|candidat|prospect|team|customer|colleague)", n):
        return brut, (a, b)
    # nom propre : chaque mot gardé commence par une majuscule
    mots = brut.split()
    garde = []
    for w in mots:
        if w[:1].isupper():
            garde.append(w)
        else:
            break
    if not garde:
        return "", None
    brut = " ".join(garde)
    return brut, (a, a + len(brut))


def detect(texte, contexte_assistant_actif=False):
    """Demande reconnue dans la dictée, ou None.

    contexte_assistant_actif : l'application au premier plan est une IA ou un terminal ;
    les consignes (mode COMPOSE, commandes) y sont ignorées, les formats déclarés restent."""
    orig = (texte or "").strip()
    if len(orig) < 6 or len(orig) > 12000:
        return None
    n = normaliser(orig)

    # 1) Réponse à un texte copié
    m = _REPONSE.match(n)
    if m and _nb_mots(m.group("corps")) >= 2 and not contexte_assistant_actif:
        return Demande(kind="reponse", livrable="reponse", mode="compose", contenu=orig[m.start("corps"):].strip(), marqueur=orig[:m.start("corps")].strip(),
                       ou="tete", source="presse-papiers", **_mods(n))

    # 2) Livrable en tête
    for fam, rx in _TETES:
        m = rx.match(n)
        if not m:
            continue
        reste_n = n[m.end():]
        reste = orig[m.end():].strip()
        sep = (m.group("sep") or "").strip()
        liv = livrable_de(m.group("liv"))
        if not liv or _nb_mots(reste) < 3:
            continue
        if _AU_SUJET_DE.match(reste_n):
            continue
        # « fais LA liste des participants », « rédige LE compte rendu » : l'article défini désigne une chose
        # qui existe, confiée à quelqu'un ; on demande à l'assistant « UNE liste », « UN mail ».
        if fam in ("cree", "poli") and sep != ":" and re.search(r"(?:^|\s)(?:le|la|les|l')\s*(?:\w+\s+){0,2}$", n[:m.start("liv")]):
            continue
        dest, _ = _dest(orig, m)
        description = bool(_INTRO_DESCRIPTION.match(reste_n)) and not sep
        if fam == "declare":
            # « c'est un mail qui … » parle d'un mail ; seul « c'est un mail : … » en annonce un
            if not sep or sep in "?":
                continue
            mode = "format"
        elif fam in ("convertit", "forme"):
            mode = "format"
        elif fam == "objectif" and sep in (":", ".") and not _INTRO_DESCRIPTION.match(reste_n.lstrip(" .:")):
            mode = "format"
        elif fam in ("cree", "poli", "objectif"):
            demonstratif = bool(re.search(r"\b(?:ce|cet|cette|ces|this)\s*$", n[:m.start("liv")].strip() + " ")) or \
                re.search(r"\b(?:ce|cet|cette|ces|this)\s+(?:\w+\s+)?$", n[:m.start("liv")]) is not None
            avec_ca = bool(re.search(r"\b(?:avec|a partir de|from|with)\s+(?:ca|cela|tout ca|tout cela|ce texte|it|this|that)\s*$", n[:m.start("sep")] if m.group("sep") is not None else ""))
            if demonstratif or avec_ca or (sep in (":",) and not description):
                mode = "format"
            else:
                mode = "compose"
        else:
            mode = "format"
        if mode == "compose":
            if contexte_assistant_actif or _SUITE_HUMAINE.search(reste_n):
                return None
            # la description est ce qui suit le livrable, marqueur compris pour le modèle
            contenu = orig.strip()
        else:
            contenu = reste
        qual = n[m.end("liv"):m.start("dest") if ("dest" in m.re.groupindex and m.group("dest")) else m.end()].strip(" ,.:;")
        d = Demande(kind="livrable", livrable=liv, mode=mode, dest=dest, qualif=qual, contenu=contenu,
                    marqueur=orig[:m.end()].strip(), ou="tete", famille=fam)
        # modificateurs dans le marqueur ou en queue (« c'est un mail pour Marie : … , en anglais »)
        d.update(_mods(n[:m.end()]))
        mq = _Q_MODS.search(n[m.end():])
        if mq and _nb_mots(n[m.end():m.end() + mq.start()]) >= 3:
            d.update(_mods(mq.group("mod")))
            d["contenu"] = orig[m.end():m.end() + mq.start()].strip(" ,;") if mode == "format" else d["contenu"]
        return d

    # 3) Livrable en queue (la dictée EST le texte)
    for fam, rx in _QUEUES:
        m = rx.search(n)
        if not m:
            continue
        corps = orig[:m.start()].strip(" ,;:")
        if _nb_mots(corps) < 3:
            continue
        liv = livrable_de(m.group("liv"))
        if not liv:
            continue
        dest, _ = _dest(orig, m)
        d = Demande(kind="livrable", livrable=liv, mode="format", dest=dest, contenu=corps, marqueur=orig[m.start():].strip(" ,;:"),
                    ou="queue", famille=fam)
        d.update(_mods(n[m.start():]))
        # une langue ou « plus court » juste avant (« …, en anglais, sous forme de mail »)
        mq = _Q_MODS.search(n[:m.start()])
        if mq and _nb_mots(n[:mq.start()]) >= 3:
            d.update(_mods(mq.group("mod")))
            d["contenu"] = orig[:mq.start()].strip(" ,;:")
        return d

    # 4) Modificateurs seuls (« …, en anglais. », « en anglais : … », « …, plus court »)
    m = _Q_MODS.search(n)
    if m and _nb_mots(n[:m.start()]) >= 3:
        mods = _mods(m.group("mod"))
        if mods:
            return Demande(kind="transforme", livrable=None, mode="format", contenu=orig[:m.start()].strip(" ,;:"),
                           marqueur=orig[m.start():].strip(" ,;:"), ou="queue", **mods)
    m = _T_MODS.match(n)
    if m and _nb_mots(n[m.end():]) >= 3:
        mods = _mods(m.group("mod"))
        if mods:
            return Demande(kind="transforme", livrable=None, mode="format", contenu=orig[m.end():].strip(),
                           marqueur=orig[:m.end()].strip(" ,;:"), ou="tete", **mods)

    if contexte_assistant_actif:
        return None

    # 5) Transformation d'un texte copié (« traduis ça en anglais », « résume ce texte »)
    m = _TRANSFO.match(n)
    if m and not _SUITE_HUMAINE.search(m.group("reste")):
        v = m.group("verbe")
        d = Demande(kind="transforme", livrable=None, mode="format", contenu="", marqueur=orig, ou="tete", source="presse-papiers", verbe=v)
        d.update(_mods(n))
        if re.match(r"(?:traduis|traduire|traduises|translate)", v) and "langue" not in d:
            d["langue"] = "auto"
        if re.match(r"(?:resume|resumer|resumes|raccourcis|raccourcir|synthetise|synthetiser|summarize|summarise|shorten)", v):
            d["court"] = True
        if re.match(r"(?:reformule|reformuler|reformules|ameliore|ameliorer|ameliores|rewrite|improve)", v) and not d.get("formel") and not d.get("court") and "langue" not in d:
            d["reformule"] = True
        return d

    # 6) Commande générale (« donne-moi trois idées de titres… », « explique… »)
    if _nb_mots(orig) >= 4 and len(orig) <= 1200:
        m = _CMD.match(n)
        if m and not _SUITE_HUMAINE.search(m.group("reste")) and not _AU_SUJET_DE.match(m.group("reste")):
            v = m.group("verbe")
            faible = v in ("fais", "fait", "faire", "donne", "donner", "trouve", "trouver", "make", "give", "find", "sors", "sortir")
            if not faible or re.search(r"\b(?:un|une|des|deux|trois|quatre|cinq|six|sept|huit|dix|quelques|plusieurs|\d+|me|moi|a|an|some|two|three|four|five|ten|few|several)\b", m.group("avant") or ""):
                return Demande(kind="commande", livrable=None, mode="compose", objet=m.group("objet"), contenu=orig, marqueur=orig, ou="tete", **_mods(n))
        m = _CMD_EXPLIQUE.match(n)
        if m and not _SUITE_HUMAINE.search(m.group("reste")):
            return Demande(kind="commande", livrable=None, mode="compose", objet="explication", contenu=orig, marqueur=orig, ou="tete", **_mods(n))
    return None
