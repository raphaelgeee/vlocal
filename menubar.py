#!/usr/bin/env python3
"""
Vlocal — Icône menu bar macOS (NSStatusItem via pyobjc).

Volontairement SANS rumps : rumps instancie son propre NSApplicationDelegate et
appelle son propre app.run() → conflit avec pywebview qui possède déjà le NSApp.
Ici on attache un NSStatusItem au NSApplication.sharedApplication() existant.

À instancier sur le MAIN THREAD Cocoa, le NSApp déjà initialisé (le thread du
hook webview.start(func=...) n'EST PAS le main thread : c'est app.py qui
dispatche la création via NSOperationQueue.mainQueue()). refresh() se marshalle
seul sur le main thread et reste donc appelable depuis n'importe quel thread.
Les callbacks Cocoa s'exécutent sur le main thread : leur Python doit déléguer
tout travail bloquant à un thread daemon.
"""

import os
import sys

import objc
from AppKit import (
    NSImage,
    NSMenu,
    NSMenuItem,
    NSStatusBar,
    NSVariableStatusItemLength,
)
from Foundation import NSObject, NSOperationQueue, NSSize, NSThread

# v16 — Base ressources frozen-aware (dev = dossier script ; .app = _MEIPASS)
if getattr(sys, "frozen", False):
    _BASE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
else:
    _BASE = os.path.dirname(os.path.abspath(__file__))


def _load_template_image_from_pngs(pt: int = 18):
    """v10 — Charge le glyphe V depuis 2 PNG pré-rastérisés (assets/) en NSImage
    template multi-rep (Retina @1x + @2x).

    Diagnostic v9 → v10 :
      - v9 : `_NSSVGImageRep` ne rend pas dans NSStatusItem à petite taille
        → glyphe invisible côté utilisateur, malgré introspection OK.
      - v10 (tentative 1) : lockFocus() en rasterisation dynamique a échoué :
        `NSWindow should only be instantiated on the main thread` — pywebview
        possède déjà le run loop, NSImage.lockFocus crée un NSWindow caché.
      - v10 (final) : on PRÉ-rastérise les PNG hors NSApp (une fois, au build)
        et on charge des bitmap statiques au runtime — aucun risque thread."""
    base = os.path.join(_BASE, "assets")
    p1x = os.path.join(base, f"vocal-glyph-v-{pt}.png")
    p2x = os.path.join(base, f"vocal-glyph-v-{pt*2}.png")
    if not os.path.exists(p1x):
        return None
    img = NSImage.alloc().initWithContentsOfFile_(p1x)
    if img is None or not img.isValid():
        return None
    # Ajoute la rep @2x pour Retina (macOS choisit la bonne selon écran)
    if os.path.exists(p2x):
        from AppKit import NSBitmapImageRep
        from Foundation import NSData
        data = NSData.dataWithContentsOfFile_(p2x)
        rep2x = NSBitmapImageRep.imageRepWithData_(data)
        if rep2x is not None:
            rep2x.setSize_(NSSize(pt, pt))  # même taille logique → résolution @2x
            img.addRepresentation_(rep2x)
    img.setSize_(NSSize(pt, pt))
    img.setTemplate_(True)
    return img


class _Delegate(NSObject):
    """Bridge des actions Cocoa -> callbacks Python."""

    def initWithCb_(self, cb):
        self = objc.super(_Delegate, self).init()
        if self is None:
            return None
        self._cb = cb
        return self

    def actDictate_(self, sender):
        self._cb.get("dictate", lambda: None)()

    def actOpen_(self, sender):
        self._cb.get("open", lambda: None)()

    def actToggle_(self, sender):
        try:
            tid = int(sender.representedObject())
        except (TypeError, ValueError):
            return
        self._cb.get("toggle_task", lambda _x: None)(tid)

    def actSettings_(self, sender):
        self._cb.get("settings", lambda: None)()

    def actQuit_(self, sender):
        self._cb.get("quit", lambda: None)()


class MenuBar:
    """Item de menu bar Vlocal. Garde une référence FORTE au delegate
    (sinon pyobjc le libère et les actions plantent)."""

    def __init__(self, callbacks: dict, labels: dict = None):
        self._delegate = _Delegate.alloc().initWithCb_(callbacks)
        self._labels = dict(labels or {})   # v3.3 — libellés i18n (fr/en) poussés par app.py
        self._item = NSStatusBar.systemStatusBar().statusItemWithLength_(
            NSVariableStatusItemLength
        )
        btn = self._item.button()
        # v10 — Glyphe V chargé depuis PNG pré-rastérisés (pas de lockFocus runtime).
        img = _load_template_image_from_pngs(pt=18)
        if img is not None:
            btn.setImage_(img)
            print("[menubar] glyphe V chargé (assets/vocal-glyph-v-18.png + @2x)")
        else:
            # Repli ultime : SF Symbol "waveform", sinon lettre V
            sym = None
            try:
                sym = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
                    "waveform", "Vlocal"
                )
            except Exception:
                sym = None
            if sym is not None:
                sym.setTemplate_(True)
                btn.setImage_(sym)
                print("[menubar] SF Symbol fallback (glyphe SVG indisponible)")
            else:
                btn.setTitle_("V")
                print("[menubar] titre 'V' fallback")

        self.refresh(tasks=[])

    # ------------------------------------------------------------------ #
    def icon_frame(self):
        """Frame ÉCRAN (x, y, w, h) de l'icône V dans la barre des menus, pour
        ancrer le bec de la bulle de dictée dessous. None si indisponible."""
        try:
            win = self._item.button().window()
            f = win.frame()
            return (float(f.origin.x), float(f.origin.y),
                    float(f.size.width), float(f.size.height))
        except Exception:
            return None

    # ------------------------------------------------------------------ #
    def refresh(self, tasks=None, labels=None):
        """Reconstruit le menu (5 derniers rappels non faits).

        Appelable depuis N'IMPORTE QUEL thread : AppKit n'est pas thread-safe,
        donc toute la construction (NSMenu/NSMenuItem/setMenu_) est marshalée
        sur le main thread Cocoa — synchrone si on y est déjà (préserve la
        sémantique des appels main-thread : __init__, actions de menu)."""
        tasks = list(tasks or [])[:5]
        if labels is not None:
            self._labels = dict(labels)

        def _build():
            # Exceptions avalées (comme le faisait le try/except des appelants
            # côté app.py) : en asynchrone, rien ne doit s'échapper du bloc.
            try:
                menu = NSMenu.alloc().init()

                for title, sel in [
                    (self._labels.get("dictate", "Dicter"), "actDictate:"),
                    (self._labels.get("open", "Ouvrir Vlocal"), "actOpen:"),
                ]:
                    it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, sel, "")
                    it.setTarget_(self._delegate)
                    menu.addItem_(it)
                menu.addItem_(NSMenuItem.separatorItem())

                # --- Rappels récents ---
                head = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                    self._labels.get("reminders", "Rappels récents"), "", ""
                )
                head.setEnabled_(False)
                menu.addItem_(head)
                if not tasks:
                    it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                        self._labels.get("none", "  (aucune)"), "", ""
                    )
                    it.setEnabled_(False)
                    menu.addItem_(it)
                else:
                    for t in tasks[:5]:
                        # État « fait » via la COCHE NATIVE AppKit (setState_) — pas de
                        # symbole/emoji dans le libellé (règle UI stricte), et accessible.
                        label = (t.get("titre") or "")[:45]
                        it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                            label, "actToggle:", ""
                        )
                        it.setState_(1 if t.get("statut") == "done" else 0)
                        it.setTarget_(self._delegate)
                        it.setRepresentedObject_(str(t.get("id")))
                        menu.addItem_(it)

                menu.addItem_(NSMenuItem.separatorItem())
                # v12c — Réglages (le glossaire est édité DANS le panneau, pas dans TextEdit)
                it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                    self._labels.get("settings", "Réglages..."), "actSettings:", ","
                )
                it.setTarget_(self._delegate)
                menu.addItem_(it)

                menu.addItem_(NSMenuItem.separatorItem())
                it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                    self._labels.get("quit", "Quitter Vlocal"), "actQuit:", "q"
                )
                it.setTarget_(self._delegate)
                menu.addItem_(it)

                self._item.setMenu_(menu)
            except Exception:
                pass

        if NSThread.isMainThread():
            _build()
        else:
            NSOperationQueue.mainQueue().addOperationWithBlock_(_build)
