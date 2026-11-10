"""Lot propreté — l'API appelée par le tableau de bord ne change pas.

pywebview expose au JavaScript toute méthode publique de l'objet `js_api`
(webview.util.get_functions parcourt dir(obj), héritage compris). Le découpage
d'app.py en modules ne doit ni retirer, ni renommer, ni changer la signature
d'une seule de ces méthodes : la page les appelle par leur nom.
"""
import inspect
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Relevé sur app.Api avant le découpage (commit 32caed7) : nom -> paramètres.
METHODES = {
    'add_glossary_entry': ('incorrect', 'correct', 'notes'),
    'add_snippet': ('trigger', 'expansion'),
    'arm_hotkey': (),
    'cancel_dictation': (),
    'check_updates': (),
    'clear_history': ('scope',),
    'connect_obsidian': (),
    'copy_text': ('text',),
    'create_obsidian_vault': (),
    'delete_glossary_entry': ('entry_id',),
    'delete_known_voice': ('voice_id',),
    'delete_meeting': ('mid',),
    'delete_snippet': ('snippet_id',),
    'delete_task': ('task_id',),
    'download_models': (),
    'engine_ready': (),
    'export_meeting': ('mid',),
    'get_diagnostics': (),
    'get_history': ('limit',),
    'get_meeting': ('mid',),
    'get_mode': (),
    'get_storage_info': (),
    'hide_window': (),
    'import_meeting_audio': ('filename', 'base64_data', 'num_speakers'),
    'is_reunion_enabled': (),
    'is_visio_available': (),
    'list_glossary': (),
    'list_input_devices': (),
    'list_known_voices': (),
    'list_meetings': (),
    'list_snippets': (),
    'list_tasks': (),
    'load_glossary': (),
    'load_settings': (),
    'models_need_download': (),
    'notif_status': (),
    'obsidian_state': (),
    'open_notif_settings': (),
    'open_perm_settings': ('pane',),
    'open_settings': (),
    'open_url': ('url',),
    'perms_state': (),
    'pick_obsidian_vault': (),
    'rediarize_meeting': ('mid', 'num_speakers'),
    'relaunch': (),
    'rename_known_voice': ('voice_id', 'name'),
    'rename_speaker': ('mid', 'speaker_id', 'name'),
    'rename_task': ('task_id', 'new_title'),
    'repair_insertion': (),
    'retry_meeting': ('mid', 'num_speakers'),
    'reunion_start': ('num_speakers', 'mode'),
    'reunion_stop': (),
    'save_glossary': ('text',),
    'save_identity': ('first_name', 'last_name', 'email', 'enabled'),
    'save_settings': ('patch',),
    'send_support_diagnostic': ('note', 'email', 'source'),
    'set_audio_device': ('index',),
    'set_history_scope': ('scope',),
    'set_lang': ('lang',),
    'set_meeting_panel': ('is_open',),
    'set_mode': ('mode',),
    'start_dictation': (),
    'stop_dictation': (),
    'telemetry_state': (),
    'test_notification': (),
    'toggle_task': ('task_id',),
    'transcribe_dropped_audio': ('filename', 'base64_data'),
    'transcribe_file': ('file_path', '_cleanup_dir'),
    'update_apply': (),
    'update_meeting_text': ('mid', 'text'),
    'update_prepare': (),
    'update_speaker_block_text': ('mid', 'block_index', 'text'),
    'usage_stats': (),
}
# Attributs publics qui ne sont pas des méthodes (constantes de classe).
ATTRIBUTS = {"MAX_AUDIO_BYTES", "SUPPORTED_AUDIO_EXT"}


class TestApiExposee(unittest.TestCase):
    def test_methodes_identiques(self):
        import app
        api = app.Api
        relevees, attributs = {}, set()
        for nom in dir(api):
            if nom.startswith("_"):
                continue
            attr = getattr(api, nom)
            if inspect.isfunction(attr):
                relevees[nom] = tuple(inspect.signature(attr).parameters)[1:]
            else:
                attributs.add(nom)
        self.assertEqual(relevees, METHODES)
        self.assertEqual(attributs, ATTRIBUTS)


if __name__ == "__main__":
    unittest.main()
