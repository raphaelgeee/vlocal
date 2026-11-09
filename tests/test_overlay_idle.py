"""Pilule au repos : aucune boucle d'animation.

Le script de dictation_overlay.html est exécuté pour de vrai dans JavaScriptCore
(pyobjc, macOS), sur un DOM minimal : on compte les images demandées
(requestAnimationFrame) en attente après chaque appel du contrat."""
import os
import re
import unittest

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

try:
    import JavaScriptCore
except ImportError:          # hors macOS
    JavaScriptCore = None

DOM = r"""
var __now = 0, __reduced = false;
var __raf = {next: 1, pending: {}}, __timers = {}, __tn = 1;
function El(tag){
  var self = this;
  this.tagName = tag; this.children = []; this.attrs = {}; this.style = {transform: ''};
  this.textContent = ''; this.className = '';
  this._cls = {};
  this.classList = {add: function(c){ self._cls[c] = 1; }, remove: function(c){ delete self._cls[c]; },
                    contains: function(c){ return !!self._cls[c]; }};
}
El.prototype.setAttribute = function(k, v){ this.attrs[k] = String(v); };
El.prototype.getAttribute = function(k){ return k in this.attrs ? this.attrs[k] : null; };
El.prototype.removeAttribute = function(k){ delete this.attrs[k]; };
El.prototype.appendChild = function(c){ this.children.push(c); return c; };
var __els = {};
['hint', 'ctitle', 'csub', 'done', 'status', 'wave'].forEach(function(id){ __els[id] = new El('div'); });
var document = {body: new El('body'), documentElement: new El('html'),
                getElementById: function(id){ return __els[id] || null; },
                createElement: function(t){ return new El(t); }};
document.body.setAttribute('data-state', 'idle');
var window = this;
var performance = {now: function(){ return __now; }};
window.matchMedia = function(q){ return {matches: __reduced}; };
window.requestAnimationFrame = function(cb){ var id = __raf.next++; __raf.pending[id] = cb; return id; };
window.cancelAnimationFrame = function(id){ delete __raf.pending[id]; };
var setTimeout = function(f, ms){ var id = __tn++; __timers[id] = f; return id; };
var clearTimeout = function(id){ delete __timers[id]; };
function __pump(n){ for(var i = 0; i < n; i++){ __now += 16; var p = __raf.pending; __raf.pending = {};
  for(var k in p) p[k](__now); } }
function __rafs(){ return Object.keys(__raf.pending).length; }
function __timersPending(){ return Object.keys(__timers).length; }
function __bar(i){ return __els.wave.children[i].style.transform; }
"""


def _script():
    with open(os.path.join(RACINE, "dictation_overlay.html"), encoding="utf-8") as f:
        return re.search(r"<script>(.*?)</script>", f.read(), flags=re.S).group(1)


@unittest.skipIf(JavaScriptCore is None, "JavaScriptCore (pyobjc) : macOS")
class TestRepos(unittest.TestCase):
    def charger(self, reduced=False):
        self.ctx = JavaScriptCore.JSContext.alloc().init()
        self.js(DOM + ("__reduced = true;" if reduced else ""))
        self.js(_script())

    def js(self, code):
        val = self.ctx.evaluateScript_(code)
        exc = self.ctx.exception()
        if exc is not None:
            self.ctx.setException_(None)
            self.fail("exception JS : %s" % exc.toString())
        return val

    def rafs(self):
        return self.js("__rafs()").toInt32()

    def test_rien_ne_tourne_apres_chargement(self):
        self.charger()
        self.assertEqual(self.rafs(), 0)
        self.assertEqual(self.js("__timersPending()").toInt32(), 0)

    def test_la_boucle_ne_tourne_que_pendant_l_ecoute(self):
        self.charger()
        self.js("ovRecording()")
        self.js("__pump(3)")
        self.assertEqual(self.rafs(), 1)
        for fin in ("ovReset()", "ovFadeOut()", "ovTranscribing(1.2)", "ovResult(0.4,true,null,'x')",
                    "ovTooShort()", "ovTooShortTap()", "ovError('')", "ovMicNeeded()", "ovInfo('a','b')"):
            self.js("ovRecording(); __pump(2)")
            self.assertEqual(self.rafs(), 1, fin)
            self.js(fin)
            self.js("__pump(5)")
            self.assertEqual(self.rafs(), 0, "boucle encore active après " + fin)
        self.js("ovReset()")
        self.assertEqual(self.js("__timersPending()").toInt32(), 0)

    def test_onde_lissee_suit_le_niveau(self):
        self.charger()
        self.js("ovRecording(); __pump(2)")
        repos = self.js("__bar(9)").toString()
        self.js("ovLevel(0.2); __pump(1)")
        une_image = float(re.search(r"[\d.]+", self.js("__bar(9)").toString()).group())
        self.js("__pump(20)")
        etabli = float(re.search(r"[\d.]+", self.js("__bar(9)").toString()).group())
        self.assertNotEqual(repos, self.js("__bar(9)").toString())
        # Interpolée : une image ne fait qu'une partie du chemin.
        self.assertLess(une_image, etabli)
        # Sans nouveau niveau, l'onde retombe vers le repos.
        self.js("__pump(120)")
        retombe = float(re.search(r"[\d.]+", self.js("__bar(9)").toString()).group())
        self.assertLess(retombe, 0.2)

    def test_mouvement_reduit_sans_boucle(self):
        self.charger(reduced=True)
        self.js("ovRecording(); ovLevel(0.2)")
        self.assertEqual(self.rafs(), 0)
        self.assertNotEqual(self.js("__bar(9)").toString(), "scaleY(0.120)")

    def test_mains_libres_et_langue(self):
        self.charger()
        self.js("ovLang('en'); ovRecording(); ovLocked('Ctrl + Cmd')")
        self.assertEqual(self.js("document.body.getAttribute('data-locked')").toString(), "1")
        self.assertEqual(self.js("__els.hint.textContent").toString(), "Mic open · Ctrl + Cmd to finish")
        self.js("ovReset()")
        self.assertTrue(self.js("document.body.getAttribute('data-locked') === null").toBool())


class TestReposCSS(unittest.TestCase):
    def test_animation_infinie_reservee_a_la_transcription(self):
        with open(os.path.join(RACINE, "dictation_overlay.html"), encoding="utf-8") as f:
            css = re.search(r"<style>(.*?)</style>", f.read(), flags=re.S).group(1)
        css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
        for sel, corps in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
            if re.search(r"\banimation\s*:\s*(?!none)", corps):
                self.assertIn('[data-state="transcribing"]', sel, sel)


if __name__ == "__main__":
    unittest.main()
