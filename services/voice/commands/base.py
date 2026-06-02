"""
Базовый реестр команд.

Добавление новой команды:
    from services.voice.commands import register_command

    def is_my_command(text: str) -> bool:
        return "моя команда" in text.lower()

    def handle_my_command(text: str, ctx: CommandContext) -> bool:
        ctx.print("Выполняю!")
        return True

    register_command(is_my_command, handle_my_command)

Зарегистрировать в init_commands() в commands/__init__.py.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from core.state import AssistantState
    from services.soundcloud.client import SoundcloudClient

CommandMatcher = Callable[[str], bool]
CommandHandler = Callable[[str, "CommandContext"], bool]


@dataclass
class CommandContext:
    """Контекст выполнения команды — доступ к состоянию и сервисам."""

    state: "AssistantState"
    soundcloud: "SoundcloudClient"
    print: Callable[[str], None]


_COMMANDS: list[tuple[CommandMatcher, CommandHandler]] = []


def register_command(matcher: CommandMatcher, handler: CommandHandler) -> None:
    """Регистрирует команду. matcher(text) -> bool, handler(text, context) -> bool (handled)."""
    _COMMANDS.append((matcher, handler))


def handle_command(text: str, context: CommandContext) -> bool:
    """
    Обрабатывает текст, возвращает True если команда распознана и выполнена.
    Команды проверяются в порядке регистрации.
    """
    for matcher, handler in _COMMANDS:
        if matcher(text):
            return handler(text, context)
    return False
