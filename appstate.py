"""État partagé du processus Vlocal.

Les objets créés au démarrage (fenêtre, base, contrôleur de dictée, menu,
télémétrie, API) sont lus par presque tous les modules de l'app. Ils vivent ici,
en attributs de module, plutôt qu'en globales d'app.py : un module ne peut pas
lire une globale réaffectée dans un autre sans importer ce dernier, et aucun
module n'importe app.py (dépendances dans un seul sens).

Règle : on lit et on affecte `appstate.x` au moment de l'usage, jamais
`from appstate import x` (on figerait la valeur du moment de l'import).
Ce module n'importe rien de l'app.
"""

# Version affichée et comparée à get-latest-version. La source est APP_VERSION
# dans app.py (synchrone avec le fichier VERSION, voir docs/RELEASING.md) ;
# app.py la recopie ici à son import.
app_version = None

# Fenêtre pywebview du tableau de bord (None avant create_window).
window = None
# Suivi de visibilité : la fenêtre naît affichée, fermer la masque.
window_visible = True
# True quand l'utilisateur quitte vraiment (menu « Quitter », Cmd+Q).
quitting = False
# Garde-fou de réentrance du nettoyage de sortie (idempotent).
quit_in_progress = False

# Persistance locale (storage.Store), None si la base n'a pas pu s'ouvrir.
store = None
# Contrôleur de dictée (dictation.DictationController), posé quand le moteur est chargé.
controller = None
# Icône de la barre des menus (menubar.MenuBar), créée sur le thread principal.
menubar = None
# Planificateur de télémétrie (telemetry.Telemetry).
telemetry = None
# Instance de l'API exposée au JavaScript (api.Api).
api = None

# Langue de l'interface côté Python ("fr" | "en"), poussée par le tableau de bord.
ui_lang = "fr"
# Libellé humain du raccourci global actif (posé par hotkeys.start_global_hotkey).
hotkey_label = "Ctrl + Espace"
# Version plus récente connue (entrée « Mise à jour disponible » du menu).
update_latest = None
# Mode Réunion activé (VLOCAL_REUNION=0 le coupe ; réglage reunion_enabled).
reunion_enabled = True

# Dossiers des modèles Whisper (turbo et small), recalculés après un téléchargement.
model_dir = None
small_model_dir = None
# Paramètres du chargement différé du moteur : (dossier du modèle, threads CPU).
engine_load_params = None
# Threads CPU retenus au démarrage, réutilisés après un téléchargement des modèles.
cpu_threads = 4
