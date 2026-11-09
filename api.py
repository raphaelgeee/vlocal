"""API exposée au tableau de bord par pywebview (window.pywebview.api.*).

Api assemble les classes de base par domaine ; pywebview expose toute méthode
publique de l'instance, héritage compris. Les méthodes ne lèvent jamais vers
le pont JavaScript (ui_bridge._api_safe ou garde locale).
"""
from api_dictation import DictationApi
from api_files import FilesApi
from api_meetings import MeetingsApi
from api_settings import SettingsApi
from api_system import SystemApi


# --------------------------------------------------------------------------- #
# API exposée à l'interface (window.pywebview.api.*)
# --------------------------------------------------------------------------- #
class Api(DictationApi, FilesApi, MeetingsApi, SettingsApi, SystemApi):
    """Objet `js_api` de pywebview : chaque méthode publique est appelable par
    le tableau de bord (window.pywebview.api.<nom>). Liste figée par
    tests/test_api_exposee.py."""
