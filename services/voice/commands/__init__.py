"""
Реестр команд голосового ассистента.
Для добавления новой команды: register_command(matcher, handler).
"""

from .base import CommandContext, handle_command, register_command
from .music import register_music_commands, try_resume_playback
from .volume import register_volume_commands


def init_commands() -> None:
    """Регистрирует все встроенные команды."""
    register_volume_commands()
    register_music_commands()


__all__ = [
    "CommandContext",
    "handle_command",
    "init_commands",
    "register_command",
    "try_resume_playback",
]
