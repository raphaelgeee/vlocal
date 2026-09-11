"""v1.3.4 — Mode clair et pastille de mise à jour : garde-fous statiques.

Le mode clair repose sur un canal d'encre (rgba(var(--ink),a)) : toute couleur
« blanc translucide » écrite en dur dans la feuille du tableau de bord
redeviendrait invisible ou illisible en clair. Ce test empêche qu'on en
réintroduise une par inadvertance, et vérifie que chaque interface a bien son
crochet de thème.
"""
import os
import re
import unittest

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _lire(nom):
    with open(os.path.join(RACINE, nom), encoding="utf-8") as f:
        return f.read()


def _feuille_dashboard(html):
    """Le 2e bloc <style> porte toute la feuille du tableau de bord."""
    blocs = re.findall(r"<style>(.*?)</style>", html, flags=re.S)
    assert len(blocs) >= 2, "feuille du dashboard introuvable"
    return blocs[1]


class TestModeClairDashboard(unittest.TestCase):
    def setUp(self):
        self.html = _lire("vlocal-interface.html")
        self.css = _feuille_dashboard(self.html)

    def test_palette_claire_declaree(self):
        self.assertIn('html[data-theme="light"]{', self.css)
        for jeton in ("--ink:", "--paper:", "--bg:", "--bad:", "--chev:", "color-scheme:light"):
            self.assertIn(jeton, self.css.split('html[data-theme="light"]{', 1)[1])

    @staticmethod
    def _regles(css):
        """(sélecteur, corps) pour chaque règle, en ignorant les commentaires."""
        css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
        return re.findall(r"([^{}]+)\{([^{}]*)\}", css)

    def _corps_fautifs(self, motif, exceptions):
        fautifs = []
        for sel, corps in self._regles(self.css):
            sel = sel.strip()
            if any(ex in sel for ex in exceptions):
                continue
            if re.search(motif, corps):
                fautifs.append(sel)
        return fautifs

    def test_aucun_blanc_translucide_en_dur(self):
        # Seules exceptions : le bouton Dicter (verre sombre voulu dans les deux
        # thèmes) et les surcharges du thème clair, qui parlent de vrai blanc.
        fautifs = self._corps_fautifs(r"rgba\(255,255,255,", ("#btn-dicter", 'html[data-theme="light"]'))
        self.assertEqual(fautifs, [], "blanc translucide hors canal d'encre : " + ", ".join(fautifs))

    def test_aucun_fff_en_dur_hors_exceptions(self):
        # Le curseur du commutateur reste blanc dans les deux thèmes ; --paper est
        # blanc en clair par définition.
        fautifs = self._corps_fautifs(r"#fff\b(?!f)", ("#btn-dicter", ".tog::after", 'html[data-theme="light"]'))
        self.assertEqual(fautifs, [], "#fff hors canal d'encre : " + ", ".join(fautifs))

    def test_reglage_apparence_present_et_traduit(self):
        self.assertIn('data-theme-opt="dark"', self.html)
        self.assertIn('data-theme-opt="light"', self.html)
        for cle in ("set.theme", "set.theme.d", "set.theme.dark", "set.theme.light"):
            self.assertEqual(self.html.count('"%s":' % cle), 2, cle)   # FR + EN

    def test_theme_pose_avant_le_premier_rendu(self):
        tete = self.html.split("<body", 1)[0]
        self.assertIn("localStorage.getItem('vlocal_theme')", tete)

    def test_pastille_mise_a_jour(self):
        self.assertIn(".nav-item.has-upd::after", self.css)
        self.assertIn("function checkUpdateBadge", self.html)
        self.assertIn("6 * 3600 * 1000", self.html)


class TestModeClairOverlay(unittest.TestCase):
    def test_overlay_html(self):
        html = _lire("dictation_overlay.html")
        self.assertIn('html[data-theme="light"]{--glass:', html)
        self.assertIn("window.ovTheme=function", html)
        for sel in (".gl", ".wb", ".worklabel", ".timer", ".lockhint", ".ctext", ".cfoot", ".icon", ".badge"):
            self.assertIn('html[data-theme="light"] %s{' % sel, html, sel)

    def test_overlay_py(self):
        src = _lire("overlay.py")
        self.assertIn("def set_theme(theme):", src)
        self.assertIn("ovTheme('light')", src)


class TestPlomberieApp(unittest.TestCase):
    def test_app_pousse_le_theme_et_la_mise_a_jour(self):
        src = _lire("app.py")
        self.assertIn("overlay.set_theme(", src)
        self.assertIn('"theme" in', src)
        self.assertIn("update_version=_update_latest", src)
        self.assertIn('"update": "Mise à jour {0} disponible..."', src)
        self.assertIn("def _update_watch", src)

    def test_menubar_entree_mise_a_jour(self):
        src = _lire("menubar.py")
        self.assertIn("def actUpdate_(self, sender):", src)
        self.assertIn("update_version=None", src)
        self.assertIn('"actUpdate:"', src)


if __name__ == "__main__":
    unittest.main()
