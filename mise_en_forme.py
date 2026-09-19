"""Vlocal 1.4.2 : mise en forme déterministe d'une dictée (mail, message, liste, note, post).

Quand la personne dit « c'est un mail pour Julien : … » ou « …, mets ça en liste »,
le texte est DÉJÀ le sien, ponctué par Whisper : il n'y a rien à inventer, seulement
à structurer. On le fait ici, sans modèle : instantané (quelques millisecondes), et
sans hallucination possible, puisque chaque mot du résultat vient de la dictée, à
l'exception de la salutation et de la formule de politesse d'un courriel.

Le modèle de langue reste réservé à ce qui demande vraiment d'écrire : un prompt,
une rédaction à partir d'une description, une traduction, un texte raccourci ou
rendu formel.
"""
import re

# Hésitations de l'oral retirées (liste courte et sûre : aucun mot porteur de sens)
_HESITATIONS = re.compile(r"(?i)(?<![\w'-])(?:euh+|heu+|hum+|hmm+|bah|ben|hein)(?![\w'-])[\s,]*")
_TICS = re.compile(r"(?i)[\s,]*(?<![\w'-])(?:tu vois|vous voyez|tu sais|vous savez|you know|i mean)(?![\w'-])[\s,]*(?=[\w.,;!?]|$)")
# Amorces de phrase qui n'apportent rien dans un texte écrit
_AMORCES = re.compile(r"(?i)(^|[.!?]\s+)(?:(?:alors|bon|donc|ok|okay|voila|voilà|bref|du coup|en fait|so|well|right)\s*[,.]?\s+)+")
_ESP = re.compile(r"[ \t]+")


def nettoyer_oral(texte):
    """Retire les hésitations et tics de l'oral, remet les espaces et les majuscules."""
    t = " " + (texte or "").strip() + " "
    t = _HESITATIONS.sub(" ", t)
    t = _TICS.sub(lambda m: ", " if "," in m.group(0) else " ", t)
    t = _ESP.sub(" ", t).strip()
    t = _AMORCES.sub(lambda m: m.group(1), t)
    t = re.sub(r"\s+([,.;:!?…])", r"\1", t)
    t = re.sub(r"([,;:])(?=[^\s\d])", r"\1 ", t)
    t = re.sub(r",\s*([.!?])", r"\1", t)
    t = re.sub(r"^[,;:\s]+", "", t)
    t = re.sub(r"\.{2,}(?!\.)", "…", t) if "..." not in t else t
    return majuscules(t.strip())


def majuscules(t):
    t = re.sub(r"(^|[.!?…]\s+|\n\s*)([a-zà-ÿ])", lambda m: m.group(1) + m.group(2).upper(), t)
    return t


def phrases(texte):
    """Découpe en phrases (sur . ! ? …), en gardant la ponctuation finale."""
    t = (texte or "").strip()
    if not t:
        return []
    morceaux = re.split(r"(?<=[.!?…])\s+(?=[A-ZÀ-ÝÉÈ0-9«\"(])", t)
    return [m.strip() for m in morceaux if m.strip()]


def _point(s):
    s = s.strip().rstrip(",;:")
    return s if re.search(r"[.!?…]$", s) else s + "."


def _mots(s):
    return len(re.findall(r"\w+", s or ""))


def langue(texte):
    w = re.findall(r"[a-zàâçéèêëîïôûùüÿœ']+", (texte or "").lower())
    en = sum(1 for x in w if x in _EN)
    fr = sum(1 for x in w if x in _FR)
    return "en" if en > fr * 1.5 and en >= 2 else "fr"


_EN = set("the and with for that this from your you are will can would should about into their there to is a of in on at my me i we our "
          "it be have has thanks thank hello hi please".split())
_FR = set("le la les des une et pour que qui dans avec sur pas est sont vous nous votre cette je tu il on un de du au merci bonjour".split())


def registre(texte):
    """'vous', 'tu' ou None selon ce que la personne a employé."""
    t = " " + (texte or "").lower() + " "
    vous = len(re.findall(r"(?<![\w'])(?:vous|votre|vos)(?![\w'])", t))
    tu = len(re.findall(r"(?<![\w'])(?:tu|te|t'|ton|ta|tes|toi)(?![\w'])", t))
    if vous > tu:
        return "vous"
    if tu > vous:
        return "tu"
    return None


# --------------------------------------------------------------------------- #
# Courriel
# --------------------------------------------------------------------------- #
_SALUT = re.compile(r"(?i)^(?P<salut>(?:bonjour|bonsoir|salut|hello|hi|hey|coucou|cher|chère|chers|chères|madame|monsieur|dear|good morning)"
                    r"(?:\s+(?:à tous|à toutes|tout le monde|l'équipe|everyone|all|team))?"
                    r"(?:\s+[A-ZÀ-Ý][\w'-]+(?:\s+[A-ZÀ-Ý][\w'-]+){0,2})?)\s*(?P<ponct>[,.!]?)\s*")
_FORMULES = re.compile(r"(?i)^(?:bonne journée|belle journée|bonne soirée|bonne fin de journée|bon week-end|bonne semaine|à bientôt|à très vite|"
                       r"à très bientôt|à plus|à tout à l'heure|à demain|merci d'avance|merci par avance|merci beaucoup|merci encore|merci|"
                       r"bien à vous|bien à toi|cordialement|bien cordialement|très cordialement|belle soirée|je t'embrasse|bises|amicalement|"
                       r"au plaisir|best|best regards|kind regards|regards|thanks|thank you|cheers|talk soon|see you)[\s,.!]*$")


_COUPE_MAIL = re.compile(r",\s+(?=(?:par contre|en revanche|cependant|d'ailleurs|sinon|du coup|mais|however|but)\b)", re.I)


def mail(corps, dest="", signature=""):
    t = nettoyer_oral(corps)
    en = langue(t) == "en"
    ph = phrases(t)
    if not ph:
        return ""
    # salutation : celle qui a été dictée, sinon une salutation simple avec le prénom
    m = _SALUT.match(ph[0])
    if m and _mots(m.group("salut")) <= 4:
        salut = m.group("salut").strip()
        salut = salut[0].upper() + salut[1:]
        reste = ph[0][m.end():].strip()
        ph = ([majuscules(reste)] if reste and _mots(reste) >= 1 else []) + ph[1:]
    else:
        salut = ("Hello %s" % dest if en else "Bonjour %s" % dest) if dest else ("Hello" if en else "Bonjour")
    # formule de politesse : celle qui a été dictée en dernière phrase, sinon une formule selon le registre
    fin = None
    if ph and _FORMULES.match(ph[-1]) and _mots(ph[-1]) <= 5:
        fin = ph.pop().rstrip(" .!")
    if not fin:
        reg = registre(t)
        fin = "Best regards" if en else ("Bien cordialement" if reg == "vous" else "Bonne journée")
    # une longue phrase de l'oral est coupée à ses articulations (« …, par contre … »)
    coupees = []
    for p in ph:
        if _mots(p) > 28:
            parts = _COUPE_MAIL.split(p)
            if len(parts) > 1:
                parts = [parts[0]] + [q[0].upper() + q[1:] for q in parts[1:] if q]
                coupees += [_point(q) for q in parts if _mots(q) >= 3] or [p]
                continue
        coupees.append(p)
    ph = coupees
    # paragraphes : une ou deux phrases, 45 mots au plus
    paras, cur = [], []
    for p in ph:
        p = _point(p)
        if cur and (len(cur) >= 2 or _mots(" ".join(cur)) + _mots(p) > 45):
            paras.append(" ".join(cur))
            cur = []
        cur.append(p)
    if cur:
        paras.append(" ".join(cur))
    out = salut + ",\n\n" + "\n\n".join(paras) + "\n\n" + fin + ","
    sig = (signature or "").strip()
    if sig:
        out += "\n" + sig
    return out


# --------------------------------------------------------------------------- #
# Message
# --------------------------------------------------------------------------- #
def message(corps, dest=""):
    t = nettoyer_oral(corps)
    return " ".join(_point(p) for p in phrases(t))


# --------------------------------------------------------------------------- #
# Liste
# --------------------------------------------------------------------------- #
_LEAD_ITEM = re.compile(r"(?i)^(?:(?:et|puis|ensuite|après|aussi|enfin|d'abord|premièrement|deuxièmement|then|and|also)\s+)*"
                        r"(?:(?:il faut que je|il faudrait que je|faut que je|je dois|je devrais|je vais|il faut|faut|il faudrait|"
                        r"je dois aussi|n'oublie pas de|ne pas oublier de|penser à|pense à|que je|que j'|i need to|i have to|i must|need to|to)\s*)?")


# « que je relise » -> « Relire » : table des verbes courants (aucune conjugaison devinée)
_INFINITIFS = {
    "rappelle": "rappeler", "appelle": "appeler", "relise": "relire", "lise": "lire", "envoie": "envoyer", "renvoie": "renvoyer",
    "fasse": "faire", "refasse": "refaire", "prenne": "prendre", "reprenne": "reprendre", "mette": "mettre", "remette": "remettre",
    "écrive": "écrire", "réécrive": "réécrire", "dise": "dire", "aille": "aller", "voie": "voir", "revoie": "revoir", "finisse": "finir",
    "choisisse": "choisir", "réfléchisse": "réfléchir", "remplisse": "remplir", "fournisse": "fournir", "sorte": "sortir", "parte": "partir",
    "vienne": "venir", "revienne": "revenir", "tienne": "tenir", "obtienne": "obtenir", "doive": "devoir", "puisse": "pouvoir",
    "sache": "savoir", "connaisse": "connaître", "paie": "payer", "paye": "payer", "essaie": "essayer", "essaye": "essayer",
    "achète": "acheter", "rachète": "racheter", "lève": "lever", "prévienne": "prévenir", "réponde": "répondre", "rende": "rendre",
    "vende": "vendre", "attende": "attendre", "descende": "descendre", "construise": "construire", "traduise": "traduire",
    "conduise": "conduire", "suive": "suivre", "boive": "boire", "reçoive": "recevoir", "vérifie": "vérifier", "étudie": "étudier",
    "copie": "copier", "modifie": "modifier", "réserve": "réserver", "prépare": "préparer", "relance": "relancer", "commande": "commander",
    "termine": "terminer", "passe": "passer", "range": "ranger", "change": "changer", "mange": "manger", "commence": "commencer",
    "annule": "annuler", "confirme": "confirmer", "contacte": "contacter", "demande": "demander", "donne": "donner", "garde": "garder",
    "imprime": "imprimer", "installe": "installer", "note": "noter", "organise": "organiser", "planifie": "planifier", "publie": "publier",
    "regarde": "regarder", "règle": "régler", "signe": "signer", "télécharge": "télécharger", "teste": "tester", "valide": "valider",
    "cherche": "chercher", "trouve": "trouver", "nettoie": "nettoyer", "écoute": "écouter", "rencontre": "rencontrer", "présente": "présenter",
    "vérife": "vérifier", "mette à jour": "mettre à jour",
}


def _infinitif(item, introduit_par_que):
    if not introduit_par_que:
        return item
    m = re.match(r"^(\S+)(.*)$", item)
    if not m:
        return item
    v = _INFINITIFS.get(m.group(1).lower())
    return (v + m.group(2)) if v else item


def liste(corps):
    t = nettoyer_oral(corps)
    t = re.sub(r"[.!?…]+(?=\s|$)", ";", t)
    items = []
    for bloc in re.split(r"\s*;\s*|,\s+|\s+(?:et puis|puis|ensuite|and then)\s+|,?\s+et\s+(?=(?:que\s|qu'|il\s|je\s|j'|d'|de\s|la\s|le\s|les\s|l'|un\s|une\s|des\s|[a-zà-ÿ]+er\b|[a-zà-ÿ]+ir\b|[a-zà-ÿ]+re\b))", t):
        b = bloc.strip(" ,.;:")
        if not b:
            continue
        que = bool(re.match(r"(?i)^(?:(?:et|puis|ensuite|aussi)\s+)*(?:il faut que je|il faudrait que je|faut que je|que je|que j')", b))
        b = _LEAD_ITEM.sub("", b).strip(" ,.;:")
        if not b:
            continue
        b = _infinitif(b, que)
        if items and _mots(b) < 2 and not re.match(r"^[A-ZÀ-Ý]", b):
            items[-1] += ", " + b
            continue
        items.append(b[0].upper() + b[1:])
    return "\n".join("- " + i for i in items)


# --------------------------------------------------------------------------- #
# Note
# --------------------------------------------------------------------------- #
def note(corps):
    t = nettoyer_oral(corps)
    ph = phrases(t)
    if not ph:
        return ""
    titre = None
    tete = re.split(r",\s+|\s*:\s+", ph[0], maxsplit=1)
    if len(tete) == 2 and 1 <= _mots(tete[0]) <= 8:
        titre = tete[0].strip(" .")
        ph[0] = tete[1]
    elif _mots(ph[0]) <= 8 and len(ph) > 1:
        titre = ph.pop(0).strip(" .")
    puces = []
    for p in ph:
        for c in re.split(r",\s+(?=(?:il|elle|on|je|j'|nous|vous|ils|elles|l'|le|la|les|c'est|ça|puis|ensuite|et|mais|donc|pour)\b)", p):
            c = c.strip(" ,.;")
            c = re.sub(r"(?i)^(?:et|puis|ensuite|mais|donc)\s+", "", c)
            if not c:
                continue
            if puces and _mots(c) < 3:
                puces[-1] += ", " + c
            else:
                puces.append(c[0].upper() + c[1:])
    out = ((titre[0].upper() + titre[1:]) + "\n") if titre else ""
    return out + "\n".join("- " + p for p in puces)


# --------------------------------------------------------------------------- #
# Publication LinkedIn
# --------------------------------------------------------------------------- #
_COUPE_POST = re.compile(r",\s+(?=(?:aujourd'hui|et|mais|donc|alors|ensuite|puis|pourtant|résultat|bref|du coup|au final|finalement|"
                         r"je|j'|on|il|elle|nous|vous|ils|elles|c'est|ce|la|le|les|mon|ma|mes|notre|nos|today|and|but|so|then|i|we|it)\b)", re.I)


def post(corps):
    t = nettoyer_oral(corps)
    lignes = []
    for p in phrases(t):
        morceaux = _COUPE_POST.split(p)
        cur = ""
        for m in morceaux:
            m = m.strip(" ,")
            if cur and _mots(cur) >= 5 and _mots(m) >= 5:
                lignes.append(_point(cur))
                cur = m[0].upper() + m[1:]
            else:
                cur = (cur + ", " + m) if cur else m
        if cur:
            lignes.append(_point(cur[0].upper() + cur[1:]))
    return "\n\n".join(lignes)


FORMATEURS = {"mail": mail, "message": message, "liste": liste, "note": note, "post": post}


def mettre_en_forme(livrable, corps, dest="", signature=""):
    """Texte mis en forme, ou None si ce livrable n'a pas de formateur déterministe."""
    f = FORMATEURS.get(livrable)
    if not f:
        return None
    if livrable == "mail":
        out = f(corps, dest, signature)
    elif livrable == "message":
        out = f(corps, dest)
    else:
        out = f(corps)
    return out.strip() or None
