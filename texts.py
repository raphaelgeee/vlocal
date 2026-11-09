"""Messages de l'app côté Python, en français et en anglais.

Les toasts, l'overlay et les erreurs renvoyées au tableau de bord sont écrits en
français au point d'appel (source unique, lisible) ; _te() les traduit à la
volée quand l'interface est en anglais (appstate.ui_lang). Les chaînes
interpolées sont traduites par préfixe (_BT_PREFIX).
"""
import appstate


# ---------------------------------------------------------------------------
# v3.3 — i18n BACKEND (fr/en). Messages utilisateur côté Python (toasts, overlay,
# erreurs renvoyées au JS). Carte de traduction INVERSE : on garde le FR au point
# d'appel (source unique, lisible), _te() traduit à la volée si la langue UI est EN.
# La langue est fixée par l'API set_lang (poussée par le dashboard au démarrage et
# au changement). Idempotent : une chaîne EN n'est pas une clé FR -> renvoyée telle
# quelle. Les chaînes interpolées (variables) restent gérées au point d'appel.
# ---------------------------------------------------------------------------
_BT = {
  "Microphone indisponible. Vérifiez Réglages Système > Confidentialité et sécurité > Microphone (et qu'aucune autre app ne le capte).":
    "Microphone unavailable. Check System Settings > Privacy & Security > Microphone (and that no other app is using it).",
  "La transcription a calé un instant (GPU), c'est réinitialisé. Relancez votre dictée, ça repart.":
    "Transcription stalled briefly (GPU) and has been reset. Start your dictation again and it picks right back up.",
  "Aucun son n'a été capté. Vérifiez que le microphone est autorisé : menu Pomme > Réglages Système > Confidentialité et sécurité > Microphone, puis autorisez Vlocal et relancez.":
    "No sound was captured. Make sure the microphone is allowed: Apple menu > System Settings > Privacy & Security > Microphone, then allow Vlocal and try again.",
  "Le son était très faible. Rapprochez-vous du micro et réessayez.":
    "The sound was very faint. Move closer to the microphone and try again.",
  "Le texte n'a pas pu être copié dans le presse-papier. Réessayez votre dictée.":
    "The text could not be copied to the clipboard. Please try your dictation again.",
  "La transcription de la réunion a échoué. L'audio est conservé, vous pouvez réessayer.":
    "The meeting transcription failed. The audio has been kept, you can try again.",
  "Le fichier de la réunion est introuvable. L'enregistrement n'a pas pu être retrouvé sur le disque.":
    "The meeting file could not be found. The recording could not be located on disk.",
  "La transcription de la réunion n'a pas pu aboutir. L'enregistrement audio est conservé, réessayez.":
    "The meeting transcription could not be completed. The audio recording has been kept, please try again.",
  "Aucune parole n'a été détectée dans cet enregistrement. Vérifiez que le micro captait bien le son.":
    "No speech was detected in this recording. Make sure the microphone was picking up sound.",
  "Réunion prête. Cliquez dessus dans la liste pour la lire ou la copier.":
    "Meeting ready. Click it in the list to read or copy it.",
  "Le moteur de transcription finit de se préparer, un instant…":
    "The transcription engine is finishing getting ready, one moment…",
  "Micro refusé. Autorisez Vlocal dans Réglages système > Confidentialité > Microphone.":
    "Microphone denied. Allow Vlocal in System Settings > Privacy > Microphone.",
  "Le mode Réunion n'est pas activé. Activez-le dans Réglages.":
    "Meeting mode is not enabled. Turn it on in Settings.",
  "La réunion précédente est encore en cours de traitement. Patientez qu'elle apparaisse comme prête dans la liste.":
    "The previous meeting is still being processed. Please wait until it shows as ready in the list.",
  "Un incident d'écriture a interrompu l'enregistrement ; la partie déjà captée va être transcrite.":
    "A write error interrupted the recording; the portion already captured will be transcribed.",
  "Enregistrement trop court, rien à transcrire.":
    "Recording too short, nothing to transcribe.",
  "Le traitement de la réunion n'a pas pu démarrer. Réessayez.":
    "The meeting could not start processing. Please try again.",
  "L'audio de cette réunion n'est plus disponible : impossible de relancer l'identification des locuteurs.":
    "This meeting's audio is no longer available, so speaker identification cannot be run again.",
  "La dictée a calé, réessayez.":
    "The dictation stalled. Please try again.",
  "La transcription a calé.":
    "The transcription stalled.",
  # --- v1.4.4 : texte vide expliqué (_EMPTY_TEXT_MSG), dictée perdue ---
  "Son trop faible. Réessayez.": "Sound too faint. Please try again.",
  "Aucune parole n'a été reconnue. Parlez distinctement, près du micro, et réessayez.":
    "No speech was recognised. Speak clearly, close to the microphone, and try again.",
  "Aucune parole reconnue. Réessayez.": "No speech recognised. Please try again.",
  "La transcription a échoué, réessayez. Si cela se répète, envoyez un diagnostic depuis les Réglages.":
    "The transcription failed. Please try again. If it keeps happening, send a diagnostic from Settings.",
  "La transcription a échoué, réessayez.": "The transcription failed. Please try again.",
  "La dictée a pris trop de temps et n'a pas pu être transcrite : elle est perdue. Réessayez votre dictée.":
    "The dictation took too long and could not be transcribed, so it was lost. Please try your dictation again.",
  "Dictée perdue (transcription trop longue). Réessayez.":
    "Dictation lost (transcription took too long). Please try again.",
  "La transcription prend trop de temps : la dictée ne sera pas insérée. Si elle aboutit, vous la retrouverez dans l'historique.":
    "The transcription is taking too long, so the dictation will not be inserted. If it completes, you will find it in the history.",
  "Trop long : dictée non insérée.": "Too long: dictation not inserted.",
  "Votre dictée a abouti trop tard pour être insérée : elle est dans l'historique.":
    "Your dictation finished too late to be inserted. It is in the history.",
  "Dictée retrouvée dans l'historique.": "Dictation saved in the history.",
  "Dictée réparée": "Dictation repaired",
  "La dernière dictée n'a pas abouti. Vous pouvez recommencer.":
    "The last dictation did not go through. You can start again.",
  "La dernière dictée n'a pas abouti. Vous pouvez recommencer, Vlocal est de nouveau prêt.":
    "The last dictation did not go through. You can start again, Vlocal is ready.",
  "Vlocal démarre": "Vlocal is starting",
  "Raccourci indisponible": "Shortcut unavailable",
  "Utilisez le bouton Dicter. Envoyez un diagnostic depuis les Réglages : le problème nous sera signalé.":
    "Use the Dictate button. Send a diagnostic from Settings: the problem will be reported to us.",
  "Le moteur se charge, réessayez dans un instant.": "The engine is loading, try again in a moment.",
  "Un instant": "One moment",
  "Connexion du micro": "Connecting the microphone",
  "Un instant, le micro se met en route.": "One moment, the microphone is starting up.",
  "Transcription précédente en cours.": "Previous transcription in progress.",
  "Réessayez.": "Please try again.",
  "Aucun son capté. Réessayez.": "No sound captured. Please try again.",
  "Échec d'insertion, réessayez.": "Insertion failed. Please try again.",
  "Insertion à autoriser": "Insertion needs permission",
  "Texte copié : Cmd + V. Autorisez Vlocal dans Réglages > Confidentialité et sécurité > Accessibilité, puis relancez Vlocal.":
    "Text copied: Cmd + V. Allow Vlocal in System Settings > Privacy & Security > Accessibility, then relaunch Vlocal.",
  "Texte copié": "Text copied",
  "Collez avec Cmd + V.": "Paste with Cmd + V.",
  "Texte copié dans le presse-papier, collez avec Cmd + V. Pour l'insertion automatique au curseur, autorisez Vlocal dans Réglages Système > Confidentialité et sécurité > Accessibilité.":
    "Text copied to the clipboard, paste with Cmd + V. For automatic insertion at the cursor, allow Vlocal in System Settings > Privacy & Security > Accessibility.",
  "Transcription du fichier en cours...": "Transcribing the file…",
  "Aucun son détectable dans le fichier.": "No detectable sound in the file.",
  "Espace disque insuffisant : l'enregistrement a été tronqué. La partie déjà captée va être transcrite.":
    "Not enough disk space: the recording was cut short. The portion already captured will be transcribed.",
  "Le moteur de transcription n'a pas pu démarrer. Réessayez de lancer Vlocal.":
    "The transcription engine could not start. Please try launching Vlocal again.",
  "Rappel": "Reminder",
  "Téléchargement des modèles échoué. Vérifiez votre connexion et réessayez.":
    "Model download failed. Check your connection and try again.",
  "Modèles téléchargés mais moteur introuvable. Réessayez.":
    "Models downloaded but the engine could not be found. Please try again.",
  "Téléchargement déjà en cours.": "Download already in progress.",
  "Espace disque insuffisant : libérez environ 3 Go puis réessayez.":
    "Not enough disk space: free up about 3 GB and try again.",
  "Réunion prête": "Meeting ready",
  # --- payloads {"error": ...} surfacés par le JS ---
  "Fichier vide.": "Empty file.",
  "Aucun fichier.": "No file.",
  "Le moteur de transcription se prépare encore, réessayez dans un instant.":
    "The transcription engine is still getting ready, try again in a moment.",
  "Import indisponible.": "Import unavailable.",
  "Le moteur de transcription se prépare encore.": "The transcription engine is still getting ready.",
  "Stockage indisponible.": "Storage unavailable.",
  "Une réunion est déjà en cours de traitement.": "A meeting is already being processed.",
  "Aucune piste audio exploitable dans le fichier.": "No usable audio track in the file.",
  "Audio trop court.": "Audio too short.",
  "Erreur interne (réunion).": "Internal error (meeting).",
  "Moteur en préparation.": "Engine getting ready.",
  "Mode Réunion désactivé.": "Meeting mode disabled.",
  "Enregistrement déjà en cours.": "Recording already in progress.",
  "Pipeline réunion précédente encore en cours.": "Previous meeting pipeline still running.",
  "Aucun enregistrement actif.": "No active recording.",
  "Enregistrement trop court.": "Recording too short.",
  "Le traitement n'a pas pu démarrer.": "Processing could not start.",
  "Le moteur se prépare encore, un instant.": "The engine is still getting ready, one moment.",
  "Réunion introuvable.": "Meeting not found.",
  "L'audio de cette réunion n'est plus disponible, réessai impossible.":
    "This meeting's audio is no longer available, retry is not possible.",
  "Un traitement est déjà en cours, réessayez dans un instant.":
    "A task is already running, try again in a moment.",
  "Export SRT impossible : cette réunion n'a pas de blocs locuteurs (diarisation absente).":
    "SRT export not possible: this meeting has no speaker blocks (no diarization).",
  # --- v1.0.13 Connexion Obsidian ---
  "Aucun coffre Obsidian détecté. Choisissez-le manuellement.":
    "No Obsidian vault detected. Choose it manually.",
  "Ce dossier n'est pas un coffre Obsidian.": "This folder is not an Obsidian vault.",
  "Connexion à Obsidian impossible.": "Could not connect to Obsidian.",
  "Module Obsidian indisponible.": "Obsidian module unavailable.",
}

# v3.3 (S3) — préfixes des messages d'erreur INTERPOLÉS (f"<préfixe> : {détail}") :
# le préfixe FR est traduit, le détail dynamique ({e}, {path}, {ext}…) est conservé.
_BT_PREFIX = {
    "Erreur de transcription : ": "Transcription error: ",
    "Format non supporté : ": "Unsupported format: ",
    "Décodage échoué : ": "Decoding failed: ",
    "Écriture tmp échouée : ": "Temporary write failed: ",
    "Fichier introuvable : ": "File not found: ",
    "Import en réunion échoué : ": "Import as meeting failed: ",
    "Réessai impossible : ": "Retry failed: ",
    "Dialogue d'enregistrement indisponible : ": "Save dialog unavailable: ",
    "Écriture impossible : ": "Could not write the file: ",
    "Impossible de démarrer l'enregistrement : ": "Couldn't start the recording: ",
}

def _te(s):
    """Traduit un message backend FR -> EN si la langue UI est EN ; sinon inchangé.
    Correspondance EXACTE d'abord (_BT), puis par PRÉFIXE (_BT_PREFIX) pour les
    messages interpolés. Idempotent (une chaîne EN n'est pas une clé FR)."""
    if appstate.ui_lang == "en" and isinstance(s, str):
        v = _BT.get(s)
        if v is not None:
            return v
        for _fr, _en in _BT_PREFIX.items():
            if s.startswith(_fr):
                return _en + s[len(_fr):]
    return s

# v1.4.4 — Dictée sans texte : (toast détaillé, texte court de l'overlay) par
# code d'incident (errors.empty_text_code). Les chaînes sont des clés de _BT.
_EMPTY_TEXT_MSG = {
    "silence": ("Le son était très faible. Rapprochez-vous du micro et réessayez.",
                "Son trop faible. Réessayez."),
    "no_speech": ("Aucune parole n'a été reconnue. Parlez distinctement, près du "
                  "micro, et réessayez.",
                  "Aucune parole reconnue. Réessayez."),
    "transcribe_fail": ("La transcription a échoué, réessayez. Si cela se répète, "
                        "envoyez un diagnostic depuis les Réglages.",
                        "La transcription a échoué, réessayez."),
    "mic_zero_signal": ("Aucun son n'a été capté. Vérifiez que le microphone est "
                        "autorisé : menu Pomme > Réglages Système > Confidentialité "
                        "et sécurité > Microphone, puis autorisez Vlocal et relancez.",
                        "Aucun son capté. Réessayez."),
}

# v1.4.4 — chien de garde de finalisation déclenché (_late_dictation_messages).
# Historique désactivé : la dictée est perdue.
_LOST_DICTATION_TOAST = ("La dictée a pris trop de temps et n'a pas pu être "
                         "transcrite : elle est perdue. Réessayez votre dictée.")

_LOST_DICTATION_OVERLAY = "Dictée perdue (transcription trop longue). Réessayez."

# Historique actif : la dictée ne sera pas insérée, mais elle y sera gardée.
_LATE_PENDING_TOAST = ("La transcription prend trop de temps : la dictée ne sera "
                       "pas insérée. Si elle aboutit, vous la retrouverez dans "
                       "l'historique.")

_LATE_PENDING_OVERLAY = "Trop long : dictée non insérée."

# Le texte tardif est arrivé et a été rangé dans l'historique.
_LATE_SAVED_TOAST = ("Votre dictée a abouti trop tard pour être insérée : "
                     "elle est dans l'historique.")

_LATE_SAVED_OVERLAY = "Dictée retrouvée dans l'historique."
