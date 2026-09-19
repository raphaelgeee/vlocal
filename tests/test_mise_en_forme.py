"""v1.4.2 : mise en forme déterministe (mise_en_forme.py). Aucun mot inventé :
tout ce qui sort vient de la dictée, sauf la salutation et la politesse d'un courriel."""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mise_en_forme as M  # noqa: E402


def mots(t):
    return set(re.findall(r"\w+", t.lower()))


class TestMail(unittest.TestCase):
    def test_structure_et_fidelite(self):
        corps = ("Alors euh, merci pour ton retour sur la proposition, tu vois on a bien regardé avec l'équipe, c'est validé, "
                 "par contre il faudrait qu'on cale un point jeudi en début d'après-midi pour les derniers détails, et je t'envoie le planning révisé d'ici là.")
        out = M.mail(corps, "Julien", "Raphael")
        self.assertTrue(out.startswith("Bonjour Julien,\n\n"))
        self.assertTrue(out.endswith("Bonne journée,\nRaphael"))
        self.assertNotIn("euh", out)
        self.assertNotIn("tu vois", out)
        self.assertIn("Merci pour ton retour sur la proposition, on a bien regardé", out)
        self.assertIn("Par contre il faudrait", out)
        ajoutes = mots(out) - mots(corps) - {"bonjour", "julien", "bonne", "journée", "raphael", "par", "contre"}
        self.assertEqual(ajoutes, set())

    def test_salutation_et_formule_dictees_gardees(self):
        out = M.mail("Hello David, j'espère que tu vas bien. Si c'est le cas, on pourrait en parler. Bonne journée.", "", "")
        self.assertTrue(out.startswith("Hello David,\n\nJ'espère que tu vas bien."))
        self.assertEqual(out.count("Bonne journée"), 1)

    def test_vouvoiement_et_anglais(self):
        self.assertIn("Bien cordialement,", M.mail("Bonjour Nabil, je vous propose un appel la semaine prochaine.", "", ""))
        out = M.mail("thanks for the feedback, the proposal is approved, we will finalise on Thursday", "Paul", "")
        self.assertTrue(out.startswith("Hello Paul,"))
        self.assertIn("Best regards,", out)


class TestListeNoteMessagePost(unittest.TestCase):
    def test_liste_infinitifs(self):
        out = M.liste("Il faut que je rappelle le comptable, que je relise le contrat de LPG avant mardi, "
                      "que j'envoie la facture à Captiv et que je réserve la salle pour la formation")
        self.assertEqual(out, "- Rappeler le comptable\n- Relire le contrat de LPG avant mardi\n- Envoyer la facture à Captiv\n"
                              "- Réserver la salle pour la formation")

    def test_liste_simple(self):
        self.assertEqual(M.liste("Acheter du lait, appeler Paul pour le devis, relire le contrat avant jeudi et prévenir Julien"),
                         "- Acheter du lait\n- Appeler Paul pour le devis\n- Relire le contrat avant jeudi\n- Prévenir Julien")

    def test_note(self):
        out = M.note("Réunion avec Fabien ce matin, il veut qu'on analyse six plans, on se revoit lundi pour la démo.")
        self.assertEqual(out.split("\n")[0], "Réunion avec Fabien ce matin")
        self.assertIn("- On se revoit lundi pour la démo", out)

    def test_message(self):
        self.assertEqual(M.message("bon je vais être en retard, commencez sans moi"), "Je vais être en retard, commencez sans moi.")

    def test_post(self):
        out = M.post("Il y a trois semaines j'ai rendu Vlocal gratuit et open source, aujourd'hui il y a des gens qui l'utilisent tous les jours.")
        self.assertEqual(out.split("\n\n")[0], "Il y a trois semaines j'ai rendu Vlocal gratuit et open source.")

    def test_rien_d_invente(self):
        corps = "Acheter du lait, appeler Paul pour le devis, relire le contrat avant jeudi"
        for liv in ("liste", "note", "message", "post"):
            out = M.mettre_en_forme(liv, corps)
            self.assertLessEqual(mots(out) - mots(corps), set(), liv)
        self.assertIsNone(M.mettre_en_forme("prompt", corps))


if __name__ == "__main__":
    unittest.main()
