"""Intelligence locale : ce qui doit être vrai sans le modèle (garde-fous, consignes, rappels, comptes rendus)."""
import unittest

import assist
import intents


def dem(texte):
    d = intents.detect(texte)
    assert d is not None, texte
    return d


class TestNettoyage(unittest.TestCase):
    def test_preambule_retire(self):
        self.assertEqual(assist.nettoyer_sortie("Voici le mail :\nBonjour Marie,\nOn valide."), "Bonjour Marie,\nOn valide.")
        self.assertEqual(assist.nettoyer_sortie("« Bonjour »"), "Bonjour")

    def test_annonce_retiree(self):
        self.assertEqual(assist.nettoyer_commande("Sure, here are three names: A, B, C."), "A, B, C.")
        self.assertTrue(assist.nettoyer_commande("Voici une liste :\n- a\n- b").startswith("- a"))

    def test_repetitif(self):
        self.assertTrue(assist.repetitif("\n".join("- Fromage %d" % i for i in range(8))))
        self.assertFalse(assist.repetitif("- lait\n- pain\n- oeufs\n- beurre\n- sel\n- poivre"))


class TestAiguillage(unittest.TestCase):
    """Mettre en forme ce qui a été dicté passe par le code ; écrire passe par le modèle."""

    def test_mise_en_forme_sans_modele(self):
        d = dem("C'est un mail pour Julien : merci pour ton retour, la proposition est validée, on cale jeudi.")
        out = assist.rediger_sans_modele(d, {"signature": "Raphael"})
        self.assertTrue(out.startswith("Bonjour Julien,"))
        self.assertTrue(out.endswith("Raphael"))
        self.assertIn("la proposition est validée", out)

    def test_redaction_passe_par_le_modele(self):
        for t in ("Rédige un prompt pour créer une image d'un train poursuivi par un avion.",
                  "C'est un prompt : je veux un plan de formation d'une journée sur l'IA pour des commerciaux",
                  "C'est un mail pour Marie : la réunion est confirmée pour mardi, en anglais."):
            self.assertIsNone(assist.rediger_sans_modele(dem(t), {}), t)

    def test_consignes(self):
        system, user, ex, mt = assist.consigne_demande(dem("Fais-moi un mail pour Marie pour décaler la réunion de jeudi."), {})
        self.assertIn("n'inventes aucun fait", system)
        self.assertIn("pour Marie", user)
        self.assertEqual(len(ex), 1)
        self.assertLessEqual(mt, 400)
        _, user, _, _ = assist.consigne_demande(dem("Write an email to Paul to confirm Tuesday's meeting at 10."), {})
        self.assertIn("in English", user)
        _, user, _, _ = assist.consigne_demande(dem("Traduis ça en anglais"), {"contexte": "Bonjour, la réunion est confirmée."})
        self.assertIn("Bonjour, la réunion est confirmée.", user)
        self.assertIn("anglais", user)


class TestGardeFous(unittest.TestCase):
    def test_consigne_et_annonce_refusees(self):
        d = dem("Rédige un prompt pour créer une image d'un train poursuivi par un avion.")
        self.assertFalse(assist.accepte_demande(d, "Voici le prompt : un train")[0])
        self.assertFalse(assist.accepte_demande(d, "Rédige un prompt pour créer une image d'un train poursuivi par un avion.")[0])
        self.assertFalse(assist.accepte_demande(d, "Je ne peux pas faire ça.")[0])

    def test_hors_sujet_refuse(self):
        d = dem("Rédige un prompt pour créer une image d'un train poursuivi par un avion.")
        self.assertFalse(assist.accepte_demande(d, "Une voiture de police en arrière-plan, lumière vive, style réaliste.")[0])
        self.assertTrue(assist.accepte_demande(d, "Un train à vapeur poursuivi par un avion, lumière du soir, plan large.")[0])

    def test_nom_propre_perdu_refuse(self):
        d = dem("Tu peux me faire un mail pour Jacques pour lui dire que je rejoins Miria le premier octobre.")
        self.assertFalse(assist.accepte_demande(d, "Bonjour Jacques,\n\nJe reviens le premier octobre.\n\nÀ bientôt,")[0])
        self.assertTrue(assist.accepte_demande(d, "Bonjour Jacques,\n\nJe rejoins Miria le premier octobre.\n\nÀ bientôt,")[0])
        d = dem("Fais-moi un prompt pour que Claude me génère un tableau de bord à partir d'un export Excel de production.")
        self.assertTrue(assist.accepte_demande(d, "Tu vas créer un tableau de bord à partir d'un export Excel de production.")[0])

    def test_registre_garde(self):
        d = dem("Fais-moi un mail avec ça : bonjour Nabil, je vous propose un appel de vingt minutes la semaine prochaine.")
        self.assertFalse(assist.accepte_demande(d, "Bonjour Nabil,\n\nJe te propose un appel de vingt minutes la semaine prochaine.")[0])

    def test_langue_gardee(self):
        d = dem("Write an email to Paul to confirm Tuesday's meeting at 10.")
        self.assertFalse(assist.accepte_demande(d, "Bonjour Paul, je confirme la réunion de mardi à 10 heures, merci.")[0])
        self.assertTrue(assist.accepte_demande(d, "Hi Paul,\n\nConfirming our meeting on Tuesday at 10.\n\nBest,")[0])

    def test_format_qui_perd_du_contenu_refuse(self):
        d = dem("C'est un prompt : je veux que tu analyses une liste de leads industriels en Île-de-France, garde celles de plus de "
                "cinq millions de chiffre d'affaires et fais un tableau avec le dirigeant et une phrase d'accroche")
        self.assertFalse(assist.accepte_demande(d, "Analyse des leads.")[0])

    def test_signature_et_destinataire_egare(self):
        d = dem("Fais-moi un mail pour Marie pour décaler la réunion de jeudi.")
        self.assertTrue(assist.signer("Bonjour Marie,\n\nOn décale.\n\nMerci,", d, {"signature": "Raphael"}).endswith("Merci,\nRaphael"))
        d = dem("Fais-moi un mail avec ça : bonjour Nabil, je vous propose un appel de vingt minutes.")
        out = assist.signer("Bonjour Nabil,\n\nJe vous propose un appel.\n\nCordialement,\nNabil", d, {"signature": "Raphael"})
        self.assertTrue(out.endswith("Cordialement,\nRaphael"))

    def test_noms_propres(self):
        self.assertEqual(assist.noms_propres("Écrire un mail pour Romain de LTC. Donc lui"), ["Romain", "LTC"])
        self.assertEqual(assist.noms_propres("Rédige un prompt pour créer une image"), [])


class TestElagage(unittest.TestCase):
    def test_sections_inventees_retirees(self):
        ref = ("je veux que tu m'aides à préparer une formation d'une journée sur l'IA pour des commerciaux dans l'industrie, "
               "avec des démos sur leurs vrais fichiers Excel et un quiz à la fin")
        out = ("Tu vas aider à préparer une formation d'une journée sur l'IA pour des commerciaux dans l'industrie.\n\n"
               "Tâche : rédige un plan de formation comprenant des démos sur leurs vrais fichiers Excel et un quiz à la fin.\n\n"
               "Contraintes : inclure des exemples concrets, adapter le contenu aux besoins, garder le langage simple et pratique.\n\n"
               "Format attendu : un plan détaillé avec les modules, les objectifs de chaque partie.")
        res = assist.elaguer_ajouts(ref, out)
        self.assertIn("Tâche : rédige", res)
        self.assertNotIn("Contraintes", res)
        self.assertNotIn("Format attendu", res)

    def test_contraintes_dictees_gardees(self):
        ref = "analyse les leads, ne m'invente rien, si tu ne trouves pas une info tu mets non trouvé, fais un tableau"
        out = "Tu analyses les leads.\n\nContraintes : n'invente rien ; si une info manque, mets « non trouvé ».\n\nFormat attendu : un tableau."
        self.assertEqual(assist.elaguer_ajouts(ref, out), out)


class TestBudget(unittest.TestCase):
    def test_budget_borne(self):
        self.assertGreater(assist.budget_redaction(360), 10)
        self.assertLessEqual(assist.budget_redaction(5000), 45)


class TestAssistSansModele(unittest.TestCase):
    def test_sans_modele_rien_n_est_invente(self):
        a = assist.Assist(lambda: "/chemin/inexistant", log=lambda *_: None)
        self.assertFalse(a.model_present())
        self.assertFalse(a.load())
        self.assertEqual(a.rediger(dem("Rédige un prompt pour créer une image d'un train."), {}), (None, "modèle absent"))
        self.assertEqual(a.rediger(dem("Traduis ça en anglais"), {}), (None, "rien de copié à retravailler"))
        self.assertIsNone(a.extraire_rappel("rappelle-moi demain à dix heures"))
        self.assertIsNone(a.compte_rendu([{"speaker": "spk_0", "text": "bonjour"}]))
        self.assertIsNone(a.compte_rendu([]))
        a.unload()


class TestRappelJson(unittest.TestCase):
    def test_rappel_valide_sans_date_calculee(self):
        r = assist.rappel_depuis_json('{"rappel": true, "titre": "appeler Paul", "quand": "demain à dix heures"}')
        self.assertEqual(r, {"titre": "Appeler Paul", "quand": "demain à dix heures"})

    def test_le_verbe_rappeler_est_retire_du_titre(self):
        r = assist.rappel_depuis_json('{"rappel": true, "titre": "rappelle-moi d\'appeler Paul", "quand": "demain"}')
        self.assertEqual(r["titre"], "Appeler Paul")

    def test_rappel_refuse(self):
        self.assertIsNone(assist.rappel_depuis_json('{"rappel": false}'))
        self.assertIsNone(assist.rappel_depuis_json('{"rappel": true, "titre": "", "quand": "demain"}'))
        self.assertIsNone(assist.rappel_depuis_json('{"rappel": true, "titre": "x", "quand": ""}'))
        self.assertIsNone(assist.rappel_depuis_json("pas du json"))

    def test_json_entoure_de_texte(self):
        r = assist.rappel_depuis_json('Voici : {"rappel": true, "titre": "Devis", "quand": "jeudi matin"} voilà.')
        self.assertEqual(r["titre"], "Devis")


class TestCompteRendu(unittest.TestCase):
    def test_extrait_valide_et_tolerant(self):
        d = assist.extrait_depuis_json('{"decisions": ["Budget validé"], "actions": [{"qui": "Gaspard", "quoi": "envoyer le planning", "quand": "vendredi"}, "relire le contrat"], '
                                       '"sujets_ouverts": [], "noms": [{"locuteur": "Locuteur 2", "prenom": "Gaspard"}, {"locuteur": "x", "prenom": "?"}]}')
        self.assertEqual(d["decisions"], ["Budget validé"])
        self.assertEqual(d["actions"][1], {"qui": "", "quoi": "relire le contrat", "quand": ""})
        self.assertEqual(d["noms"], [{"locuteur": 2, "prenom": "Gaspard"}])
        self.assertIsNone(assist.extrait_depuis_json("résumé en prose sans json"))

    def test_fusion_sans_doublon_et_vote_des_noms(self):
        e1 = {"decisions": ["Budget validé"], "actions": [{"qui": "Gaspard", "quoi": "Envoyer le planning", "quand": ""}], "sujets_ouverts": ["Prix"], "noms": [{"locuteur": 2, "prenom": "Gaspard"}]}
        e2 = {"decisions": ["budget validé"], "actions": [{"qui": "", "quoi": "envoyer le planning", "quand": "vendredi"}], "sujets_ouverts": [], "noms": [{"locuteur": 2, "prenom": "Gaspard"}, {"locuteur": 2, "prenom": "Gérard"}]}
        f = assist.fusionner_extraits([e1, e2])
        self.assertEqual(f["decisions"], ["Budget validé"])
        self.assertEqual(len(f["actions"]), 1)
        self.assertEqual(f["noms"], {"2": "Gaspard"})
        md = assist.rendre_compte_rendu(f)
        self.assertIn("## Décisions\n- Budget validé", md)
        self.assertIn("- Gaspard : Envoyer le planning", md)
        self.assertIn("## Noms entendus\n- Locuteur 2 : Gaspard", md)

    def test_sections_vides(self):
        md = assist.rendre_compte_rendu({"decisions": [], "actions": [], "sujets_ouverts": [], "noms": {}})
        self.assertEqual(md.count("Rien de noté."), 3)
        self.assertNotIn("Noms entendus", md)

    def test_libelle_locuteur(self):
        self.assertEqual(assist.libelle_locuteur("spk_0"), "Locuteur 1")
        self.assertEqual(assist.libelle_locuteur("SPEAKER_02"), "Locuteur 3")


class TestRappelHybride(unittest.TestCase):
    NOW = __import__("datetime").datetime(2026, 9, 16, 9, 30)

    def test_duree_relative_gardee(self):
        r = assist.resoudre_rappel({"titre": "Sortir le gâteau", "quand": "deux heures"},
                                   "rappelle-moi dans deux heures de sortir le gâteau", self.NOW)
        self.assertEqual(r["when"].hour, 11)

    def test_heures_en_lettres(self):
        self.assertEqual(assist.chiffrer_heures("avant dix-sept heures trente"), "avant 17h30")
        self.assertEqual(assist.chiffrer_heures("dans deux heures"), "dans deux heures")
        r = assist.resoudre_rappel({"titre": "Envoyer le contrat", "quand": "avant dix-sept heures trente"},
                                   "pense à envoyer le contrat avant dix-sept heures trente", self.NOW)
        self.assertEqual((r["when"].hour, r["when"].minute), (17, 30))

    def test_anglais_titre_garbage_remplace(self):
        r = assist.resoudre_rappel({"titre": "Rappeler l'action", "quand": "le moment exactement tel que dit"},
                                   "don't forget to send the invoice tomorrow morning", self.NOW)
        self.assertEqual(r["titre"], "Send the invoice")
        self.assertEqual(r["when"].day, 17)

    def test_echeance_hors_du_titre(self):
        r = assist.resoudre_rappel({"titre": "Call the accountant on Monday at 9", "quand": "on Monday at 9"},
                                   "remind me to call the accountant on Monday at 9", self.NOW)
        self.assertEqual(r["titre"], "Call the accountant")
        r = assist.resoudre_rappel({"titre": "Appeler Paul à 18h", "quand": "à 18h"}, "rappelle-moi d'appeler Paul à 18h", self.NOW)
        self.assertEqual(r["titre"], "Appeler Paul")

    def test_sans_date(self):
        self.assertIsNone(assist.resoudre_rappel({"titre": "X", "quand": "bientôt"}, "pense à X bientôt", self.NOW))
        self.assertIsNone(assist.resoudre_rappel(None, "x", self.NOW))


if __name__ == "__main__":
    unittest.main()
