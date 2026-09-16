"""Intelligence locale : ce qui doit être vrai sans le modèle."""
import time
import unittest

import assist


class TestIntentions(unittest.TestCase):
    def t(self, texte, attendu, contenu_contient=None, dest=None):
        intent, params, contenu = assist.detect_intent(texte)
        self.assertEqual(intent, attendu, texte)
        if contenu_contient:
            self.assertIn(contenu_contient, contenu, texte)
        if dest is not None:
            self.assertEqual(params.get("dest"), dest, texte)
        return contenu

    def test_marqueur_en_tete(self):
        c = self.t("c'est un mail pour Marie : on valide la proposition et on cale jeudi", "mail", "on valide", dest="Marie")
        self.assertFalse(c.lower().startswith("c'est un mail"))
        self.t("Alors, ceci est une note : trois points à traiter avant la démo", "note", "trois points")
        self.t("Voici un prompt : je veux un plan de formation IA pour des commerciaux", "prompt", "plan de formation")
        self.t("En anglais : bonjour Marie, la réunion est confirmée pour mardi", "en", "bonjour Marie")
        self.t("This is an email to Paul: the meeting is confirmed for Tuesday", "mail", "meeting is confirmed", dest="Paul")

    def test_marqueur_en_queue(self):
        self.t("On valide la proposition et on cale les détails jeudi, voilà, mets ça en mail.", "mail", "on cale")
        self.t("Acheter du lait, appeler Paul, relire le contrat, mets tout ça en liste", "liste", "relire le contrat")
        self.t("Le rendez-vous est confirmé pour mardi à quatorze heures en anglais", "en", "confirmé pour mardi")
        self.t("Bonjour Marie, la proposition est validée. Plus court.", "court", "proposition est validée")
        self.t("We validated the scope and the budget, make it a list", "liste", "validated the scope")
        self.t("Merci pour ton retour, je reviens vers toi demain, en français.", "fr", "je reviens")

    def test_reponse_au_texte_copie(self):
        self.t("Réponds-lui que c'est bon pour jeudi et que j'apporte les documents", "reponse", "c'est bon pour jeudi")
        self.t("reply that Thursday works for me", "reponse", "Thursday works")

    def test_pas_d_intention(self):
        for texte in ("Bonjour Marie, c'est parfait pour moi, on valide la proposition.",
                      "Je t'envoie un mail demain avec les détails.",           # « mail » au milieu : pas un marqueur
                      "Il faut traduire le contrat en anglais avant vendredi.",  # « en anglais » au milieu
                      "note",                                                    # marqueur seul, rien à mettre en forme
                      "mets ça en liste",
                      ""):
            intent, _, contenu = assist.detect_intent(texte)
            self.assertIsNone(intent, texte)
            self.assertEqual(contenu, texte)

    def test_le_contenu_garde_ses_mots(self):
        _, _, c = assist.detect_intent("c'est un message : je serai en retard de dix minutes, commencez sans moi")
        self.assertEqual(c, "je serai en retard de dix minutes, commencez sans moi")


class TestConsignes(unittest.TestCase):
    def test_signature_et_destinataire(self):
        system, user = assist.messages_pour("mail", "on valide", {"dest": "Marie", "signature": "Raphael"})
        self.assertIn("pour Marie", user)
        self.assertIn("signature « Raphael »", user)
        self.assertIn("Texte dicté :\non valide", user)
        self.assertTrue(user.index("Texte dicté") < user.index("Consigne :"), "le texte vient avant la consigne")
        self.assertIn("gardes son sens", system)

    def test_sans_signature_rien_n_est_invente(self):
        _, user = assist.messages_pour("mail", "on valide", {})
        self.assertNotIn("{signature}", user)
        self.assertNotIn("{dest}", user)
        self.assertNotIn("signature «", user)

    def test_reponse_embarque_le_texte_copie(self):
        _, user = assist.messages_pour("reponse", "c'est bon pour jeudi", {"contexte": "Peux-tu jeudi ?"})
        self.assertIn("Message reçu :\nPeux-tu jeudi ?", user)


class TestGardeFous(unittest.TestCase):
    def test_nettoyage_fidele_accepte(self):
        ok, info = assist.sortie_acceptable("propre", "euh bonjour marie c'est parfait pour moi on valide la proposition",
                                            "Bonjour Marie, c'est parfait pour moi. On valide la proposition.")
        self.assertTrue(ok, info)

    def test_nettoyage_qui_reformule_refuse(self):
        ok, info = assist.sortie_acceptable("propre", "bonjour marie c'est parfait pour moi on valide la proposition et on cale jeudi",
                                            "Chère Marie, je vous confirme mon accord total. Fixons une date.")
        self.assertFalse(ok)
        self.assertIn("trop modifié", info)

    def test_vide_et_refus_du_modele(self):
        self.assertFalse(assist.sortie_acceptable("mail", "x y z", "")[0])
        self.assertFalse(assist.sortie_acceptable("mail", "x y z", "Je ne peux pas faire cela.")[0])

    def test_plus_court_doit_etre_plus_court(self):
        self.assertFalse(assist.sortie_acceptable("court", "un deux trois", "un deux trois quatre cinq six")[0])

    def test_consigne_recopiee_refusee(self):
        # ce que le banc a produit sur une vraie dictée : la consigne devenue contenu
        ok, info = assist.sortie_acceptable("mail", "on valide la proposition et on cale les détails jeudi avec Marie",
                                            "Bonjour,\nVoici la dictée transformée en courriel :\nCher(e) interlocuteur(e), je vous transmets la transcription.")
        self.assertFalse(ok); self.assertIn("consigne recopiée", info)
        ok, info = assist.sortie_acceptable("en", "on valide la proposition et on cale les détails jeudi avec Marie",
                                            "This dictation has been translated into natural English professionally.")
        self.assertFalse(ok)

    def test_contenu_perdu_refuse(self):
        ok, info = assist.sortie_acceptable("liste", "acheter du lait, appeler Paul, relire le contrat, prévenir Julien",
                                            "- Choisir la bonne option\n- Rester fidèle\n- Ne rien changer")
        self.assertFalse(ok); self.assertIn("contenu perdu", info)
        ok, info = assist.sortie_acceptable("liste", "acheter du lait, appeler Paul, relire le contrat, prévenir Julien",
                                            "- Acheter du lait\n- Appeler Paul\n- Relire le contrat\n- Prévenir Julien")
        self.assertTrue(ok, info)

    def test_traduction_longueur(self):
        ok, _ = assist.sortie_acceptable("en", "Bonjour Marie, la réunion est confirmée pour mardi à quatorze heures.",
                                         "Hello Marie, the meeting is confirmed for Tuesday at 2 pm.")
        self.assertTrue(ok)
        self.assertFalse(assist.sortie_acceptable("en", "Bonjour Marie, la réunion est confirmée pour mardi à quatorze heures.", "Hi.")[0])

    def test_preambule_retire(self):
        self.assertEqual(assist.nettoyer_sortie("Voici le mail :\nBonjour Marie,\nOn valide."), "Bonjour Marie,\nOn valide.")
        self.assertEqual(assist.nettoyer_sortie("« Bonjour »"), "Bonjour")


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


class TestAssistSansModele(unittest.TestCase):
    def test_sans_modele_tout_retombe_sur_le_brut(self):
        a = assist.Assist(lambda: "/chemin/inexistant", log=lambda *_: None)
        self.assertFalse(a.model_present())
        self.assertFalse(a.load())
        self.assertEqual(a.transformer("mail", "bonjour", {}), (None, "modèle absent"))
        self.assertIsNone(a.extraire_rappel("rappelle-moi demain à dix heures"))
        self.assertIsNone(a.compte_rendu([{"speaker": "spk_0", "text": "bonjour"}]))
        self.assertIsNone(a.compte_rendu([]))
        a.unload()   # sans modèle : silencieux


if __name__ == "__main__":
    unittest.main()


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

    def test_sans_date(self):
        self.assertIsNone(assist.resoudre_rappel({"titre": "X", "quand": "bientôt"}, "pense à X bientôt", self.NOW))
        self.assertIsNone(assist.resoudre_rappel(None, "x", self.NOW))


class TestGardeFousIntentions(unittest.TestCase):
    def test_etiquette_titre_retiree(self):
        self.assertTrue(assist.nettoyer_sortie("Titre court : Démo\n- a").startswith("Démo"))

    def test_prompt_tolere_assistant_ia(self):
        ok, _ = assist.sortie_acceptable("prompt", "c'est un prompt : plan de formation IA pour commerciaux industrie démonstrations fichiers",
                                         "Contexte : formation IA pour des commerciaux dans l'industrie.\nTâche : plan de formation avec démonstrations sur leurs fichiers.\nContraintes : assistant IA.\nFormat attendu : plan.")
        self.assertTrue(ok)
        ok, _ = assist.sortie_acceptable("note", "réunion demain", "Note : assistant IA réunion demain")
        self.assertFalse(ok)


class TestNettoyageSansPerte(unittest.TestCase):
    def test_tete_avalee_refusee(self):
        ok, info = assist.sortie_acceptable("propre", "euh bonjour marie la réunion est euh confirmée pour mardi à quatorze heures",
                                            "La réunion est confirmée pour mardi à quatorze heures.")
        self.assertFalse(ok); self.assertIn("mots perdus", info)

    def test_nettoyage_fidele_accepte(self):
        ok, _ = assist.sortie_acceptable("propre", "euh bonjour marie la réunion est euh confirmée pour mardi à quatorze heures",
                                         "Bonjour Marie, la réunion est confirmée pour mardi à 14 heures.")
        self.assertTrue(ok)

    def test_titre_rappeler_retire(self):
        r = assist.rappel_depuis_json('{"rappel": true, "titre": "Rappeler le point avec Gaspard", "quand": "vendredi"}')
        self.assertEqual(r["titre"], "Le point avec Gaspard")
