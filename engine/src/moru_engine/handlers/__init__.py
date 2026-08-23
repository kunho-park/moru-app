"""Content handlers for extracting and applying translations.

This module provides a unified interface for handling different
file types in Minecraft modpacks. Handlers extract translatable
content and apply translations back to files.
"""

from .base import ContentHandler, HandlerRegistry
from .ftbquests import FTBQuestsHandler
from .kubejs_display_name import KubeJSDisplayNameHandler
from .language import LanguageHandler
from .origins import OriginsHandler
from .patchouli import PatchouliHandler
from .puffish_skills import PuffishSkillsHandler
from .tconstruct import TConstructHandler
from .the_vault_quest import TheVaultQuestHandler

__all__ = [
    "ContentHandler",
    "FTBQuestsHandler",
    "HandlerRegistry",
    "KubeJSDisplayNameHandler",
    "LanguageHandler",
    "OriginsHandler",
    "PatchouliHandler",
    "PuffishSkillsHandler",
    "TConstructHandler",
    "TheVaultQuestHandler",
]
