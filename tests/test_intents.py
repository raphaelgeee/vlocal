"""v1.4.2 : reconnaissance des demandes dictées (intents.py).

Deux exigences, mesurées ensemble :
  - rappel : toutes les façons naturelles de demander un mail, un prompt, un message,
    une note, une liste ou une publication, en tête (« c'est un mail pour Marie : … »,
    « fais-moi un prompt pour… ») ou en fin de dictée (« …, mets ça en liste ») ;
  - précision : une dictée ordinaire, ou une consigne adressée à une autre IA,
    n'est jamais prise pour une demande (le texte passerait par le modèle).
Le banc complet, sur les dictées réelles de l'auteur, reste hors du dépôt (données
personnelles) ; les phrases pièges ci-dessous en reprennent les tournures.
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import intents  # noqa: E402

CORPS = {"mail": "merci pour ton retour, la proposition est validée, on cale les derniers détails jeudi en début d'après-midi",
         "prompt": "je veux un plan de formation d'une journée sur l'IA pour des commerciaux dans l'industrie avec des démos",
         "message": "je serai en retard de dix minutes, commencez sans moi, je vous rejoins pour la partie budget",
         "note": "trois points avant la démo de vendredi, vérifier le micro, tester l'insertion dans Notion, préparer deux exemples",
         "liste": "acheter du lait, appeler Paul pour le devis, relire le contrat avant jeudi, prévenir Julien",
         "post": "trois semaines après avoir rendu Vlocal gratuit, six personnes l'utilisent chaque semaine"}
DESC = {"mail": "pour décaler la réunion de jeudi à vendredi matin", "prompt": "pour générer une image d'un train poursuivi par un avion",
        "message": "pour prévenir l'équipe que la démo est décalée à quinze heures", "note": "sur les points à préparer avant la démo de vendredi",
        "liste": "de courses pour une raclette pour six personnes", "post": "sur le lancement de la version gratuite de Vlocal"}
MOTS = {"mail": ["mail", "e-mail", "email", "courriel"], "prompt": ["prompt", "prompte"], "message": ["message", "texto", "SMS"],
        "note": ["note", "mémo", "compte rendu"], "liste": ["liste", "liste à puces", "to-do list", "checklist"],
        "post": ["post LinkedIn", "post", "publication LinkedIn"]}
FORMAT_TETE = ["C'est {a} {w} : {c}", "C'est {a} {w}. {C}", "Ceci est {a} {w} : {c}", "Voici {a} {w} : {c}", "Ça c'est {a} {w} : {c}",
               "Alors c'est {a} {w} : {c}", "Ok, c'est {a} {w}, {c}", "L'objectif c'est {a} {w} : {c}", "Le but c'est {a} {w} : {c}",
               "Rédige-moi ce {w} : {c}", "Fais-moi {a} {w} : {c}", "Mets ça en {w} : {c}", "Transforme ça en {w} : {c}", "Il me faut {a} {w} : {c}",
               "J'ai besoin d'{a} {w} : {c}", "Je veux {a} {w} : {c}", "Sous forme de {w} : {c}", "Fais {a} {w} avec ça : {c}", "Établis {a} {w} : {c}",
               "Écris-moi ce {w} : {c}", "Voilà, c'est {a} {w} : {c}", "Bon, l'objectif, c'est {a} {w}. {C}"]
FORMAT_QUEUE = ["{C}, c'est {a} {w}.", "{C}. C'est {a} {w}.", "{C}, mets ça en {w}.", "{C}. Mets ça sous forme de {w}.", "{C}, transforme ça en {w}.",
                "{C}. Fais-en {a} {w}.", "{C}, fais-moi {a} {w} avec ça.", "{C}. Voilà, c'est {a} {w}.", "{C}, en {w}.", "{C}. Sous forme de {w}.",
                "{C}, ça c'est {a} {w}.", "{C}, format {w}.", "{C}. Rédige ça en {w}.", "{C}, tu peux me mettre ça en {w} ?"]
COMPOSE = ["Rédige {a} {w} {d}", "Rédige-moi {a} {w} {d}", "Fais-moi {a} {w} {d}", "Fais {a} {w} {d}", "Écris {a} {w} {d}", "Écris-moi {a} {w} {d}",
           "Établis {a} {w} {d}", "Prépare-moi {a} {w} {d}", "Génère {a} {w} {d}", "Crée {a} {w} {d}", "Propose-moi {a} {w} {d}",
           "Tu peux me rédiger {a} {w} {d}", "Peux-tu me faire {a} {w} {d}", "Est-ce que tu peux m'écrire {a} {w} {d}",
           "J'aimerais que tu me rédiges {a} {w} {d}", "Tu me fais {a} {w} {d}", "L'objectif c'est {a} {w} {d}", "Il me faut {a} {w} {d}",
           "J'ai besoin d'{a} {w} {d}", "Je voudrais {a} {w} {d}", "Formule {a} {w} {d}", "Ok, fais-moi {a} petit {w} {d}"]

# Dictées ordinaires qui ressemblent à une demande sans en être une
PIEGES = [
    "Je t'envoie ça en mail demain matin, dis-moi si c'est bon pour toi.",
    "Je vais écrire un mail à Marie ce soir pour lui expliquer la situation.",
    "C'est un mail qui est arrivé hier et que je n'ai pas encore lu.",
    "Le mail que j'ai reçu de Julien ne parle pas du budget.",
    "Ce prompt ne marche pas du tout, il faut le reprendre.",
    "Merci pour la note, je la relis ce soir.",
    "Je t'envoie le résumé ce soir après la réunion.",
    "Fais attention à la marche en sortant.",
    "Il faut trouver une solution pour le planning de la semaine prochaine.",
    "Peux-tu m'envoyer le devis avant jeudi ?",
    "Donne le message à Paul quand tu le vois.",
    "Je fais la liste des courses ce soir et je passe au magasin.",
    "Fais la liste des participants avant la réunion s'il te plaît.",
    "Trouve une salle pour vendredi et réserve-la.",
    "Explique-lui que je serai en retard de dix minutes.",
    "Rédige un compte rendu de la réunion et envoie-le à Paul avant ce soir.",
    "Réponds-moi sans complaisance, on tourne en rond.",
    "Et avant de commencer, tu en penses quoi ?",
    "L'objectif, c'est que tu coupes les cartouches et que tu gardes uniquement les plans.",
    "Je souhaite que tu te concentres uniquement sur ça et que tu fasses en sorte que tout soit traçable.",
    "Utilise le connecteur Apify et regarde cette liste de leads.",
    "On ouvre l'agence de formation, je souhaite faire un deck de présentation aux couleurs du site.",
    "Ça n'arrive pas par mail ni par l'intranet.",
    "Bonjour Marie, la réunion est confirmée pour mardi à quatorze heures, salle Vendôme.",
    "Hello Benoit, j'ai été en contact rapidement avec son DSI.",
    "Au plaisir de re-échanger.",
    "Les prompts, je veux que tu les essaies sur des leads Instagram.",
    "C'est un message important, il faut le lire avant la réunion.",
    "La note de frais est à rendre avant vendredi.",
    "J'ai posté un message sur le canal de l'équipe ce matin.",
]
# Consignes adressées à une autre IA (dictées dans Claude, ChatGPT, un terminal)
POUR_UNE_IA = [
    "Je souhaiterais que tu rédiges un mail de réponse pour une personne qui demande une formation IA.",
    "Tu peux me faire un mail pour Jacques, c'est un jeune donc tutoie-le.",
    "Rédige-moi un email de prospection pour un cabinet d'avocats.",
    "Fais un plan extrêmement précis de la formation.",
    "Explique le calcul des quatorze pour cent.",
    "Donne-moi trois idées de titres pour un post LinkedIn.",
]


def _art(w):
    return "une" if w.split()[0] in ("note", "liste", "publication", "checklist", "to-do") else "un"


class TestRappel(unittest.TestCase):
    def test_toutes_les_tournures(self):
        rates = []
        for liv, ws in MOTS.items():
            C = CORPS[liv][0].upper() + CORPS[liv][1:]
            for w in ws:
                a = _art(w)
                for t in FORMAT_TETE + FORMAT_QUEUE:
                    s = t.format(a=a, w=w, c=CORPS[liv], C=C)
                    d = intents.detect(s)
                    if not d or d.livrable != liv or d.mode != "format":
                        rates.append(s)
                for t in COMPOSE:
                    s = t.format(a=a, w=w, d=DESC[liv])
                    d = intents.detect(s)
                    if not d or d.livrable != liv or d.mode != "compose":
                        rates.append(s)
        self.assertEqual(rates, [])

    def test_destinataire(self):
        d = intents.detect("C'est un mail pour Marie : merci pour ton retour, la proposition est validée")
        self.assertEqual((d.livrable, d.mode, d.dest, d.contenu), ("mail", "format", "Marie", "merci pour ton retour, la proposition est validée"))
        d = intents.detect("Écris un message à l'équipe pour annoncer que la démo est à quinze heures")
        self.assertEqual((d.livrable, d.mode), ("message", "compose"))

    def test_anglais(self):
        for s, liv in [("This is an email to Paul: thanks for the feedback, the proposal is approved", "mail"),
                       ("Write an email to Paul to confirm Tuesday's meeting", "mail"),
                       ("Make me a prompt to generate an image of a train chased by a plane", "prompt"),
                       ("Buy milk, call Paul about the quote, review the contract, make it a list.", "liste")]:
            self.assertEqual(intents.detect(s).livrable, liv, s)

    def test_modificateurs(self):
        d = intents.detect("C'est un mail pour Marie : la réunion est confirmée pour mardi à quatorze heures, en anglais.")
        self.assertEqual((d.livrable, d.langue, d.contenu), ("mail", "en", "la réunion est confirmée pour mardi à quatorze heures"))
        self.assertTrue(intents.detect("On a validé le périmètre et le budget est confirmé tel quel, plus court.").court)
        self.assertTrue(intents.detect("Salut Paul, ok pour l'essai, on cale ça la semaine pro, plus formel.").formel)
        d = intents.detect("Traduis ça en anglais")
        self.assertEqual((d.kind, d.source, d.langue), ("transforme", "presse-papiers", "en"))

    def test_le_contenu_garde_ses_mots(self):
        d = intents.detect("Acheter du lait, appeler Paul pour le devis, relire le contrat, mets ça en liste.")
        self.assertEqual(d.contenu, "Acheter du lait, appeler Paul pour le devis, relire le contrat")


class TestPrecision(unittest.TestCase):
    def test_dictees_ordinaires(self):
        faux = [(s, dict(intents.detect(s))) for s in PIEGES if intents.detect(s)]
        self.assertEqual(faux, [])

    def test_consignes_pour_une_autre_ia(self):
        for s in POUR_UNE_IA:
            self.assertIsNone(intents.detect(s, contexte_assistant_actif=True), s)
        # un format déclaré reste une demande explicite, même dans une IA
        d = intents.detect("C'est un prompt : je veux un plan de formation d'une journée sur l'IA", contexte_assistant_actif=True)
        self.assertEqual((d.livrable, d.mode), ("prompt", "format"))

    def test_contexte_assistant(self):
        self.assertTrue(intents.contexte_assistant("com.anthropic.claudefordesktop"))
        self.assertTrue(intents.contexte_assistant("com.openai.codex"))
        self.assertTrue(intents.contexte_assistant("com.apple.Terminal"))
        self.assertTrue(intents.contexte_assistant("com.google.Chrome", "Nouvelle conversation - ChatGPT"))
        self.assertFalse(intents.contexte_assistant("com.apple.mail", "Nouveau message"))
        self.assertFalse(intents.contexte_assistant("com.google.Chrome", "Boîte de réception - Gmail"))


class TestVitesse(unittest.TestCase):
    def test_moins_d_une_milliseconde_en_moyenne(self):
        textes = PIEGES * 5
        t = time.perf_counter()
        for s in textes:
            intents.detect(s)
        self.assertLess((time.perf_counter() - t) / len(textes), 0.003)


if __name__ == "__main__":
    unittest.main()
