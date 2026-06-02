"""
Голосовые команды управления громкостью.
"""

from __future__ import annotations

import re

import settings
from services.audio.ducking import set_sink_input_volume_percent, set_sink_volume_percent
from services.audio.volume_state import (
    load_baseline_volume_percent,
    save_baseline_volume_percent,
)
from services.voice.commands.base import CommandContext, register_command

_STEP_PERCENT = 10

_VOLUME_CFG = settings.VOICE_COMMANDS.get("volume", {})
_VOLUME_SET_RE = re.compile(
    str(_VOLUME_CFG.get("set_regex", settings.DEFAULT_VOICE_COMMANDS["volume"]["set_regex"])),
    re.IGNORECASE,
)
_VOLUME_UP_RE = re.compile(
    str(_VOLUME_CFG.get("up_regex", settings.DEFAULT_VOICE_COMMANDS["volume"]["up_regex"])),
    re.IGNORECASE,
)
_VOLUME_DOWN_RE = re.compile(
    str(_VOLUME_CFG.get("down_regex", settings.DEFAULT_VOICE_COMMANDS["volume"]["down_regex"])),
    re.IGNORECASE,
)

_UNITS = {
    "ноль": 0,
    "один": 1,
    "одна": 1,
    "два": 2,
    "две": 2,
    "три": 3,
    "четыре": 4,
    "пять": 5,
    "шесть": 6,
    "семь": 7,
    "восемь": 8,
    "девять": 9,
}
_TEENS = {
    "десять": 10,
    "одиннадцать": 11,
    "двенадцать": 12,
    "тринадцать": 13,
    "четырнадцать": 14,
    "пятнадцать": 15,
    "шестнадцать": 16,
    "семнадцать": 17,
    "восемнадцать": 18,
    "девятнадцать": 19,
}
_TENS = {
    "двадцать": 20,
    "тридцать": 30,
    "сорок": 40,
    "пятьдесят": 50,
    "шестьдесят": 60,
    "семьдесят": 70,
    "восемьдесят": 80,
    "девяносто": 90,
}
_HUNDREDS = {
    "сто": 100,
}


def _parse_volume_percent(raw_value: str) -> int | None:
    value = raw_value.strip().lower()
    if not value:
        return None

    value = value.replace("%", " ")
    value = re.sub(r"\bпроцент(?:а|ов)?\b", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    if not value:
        return None

    if value.isdigit():
        return int(value)

    total = 0
    tokens = value.split()
    for token in tokens:
        if token in _HUNDREDS:
            total += _HUNDREDS[token]
        elif token in _TENS:
            total += _TENS[token]
        elif token in _TEENS:
            total += _TEENS[token]
        elif token in _UNITS:
            total += _UNITS[token]
        else:
            return None
    return total


def is_volume_command(text: str) -> bool:
    normalized = text.strip().lower()
    if not normalized:
        return False
    m = _VOLUME_SET_RE.match(normalized)
    if m:
        return _parse_volume_percent(m.group(1)) is not None
    return bool(
        _VOLUME_UP_RE.match(normalized)
        or _VOLUME_DOWN_RE.match(normalized)
    )


def _apply_volume(percent: int, context: CommandContext) -> None:
    percent = max(0, min(100, int(percent)))
    save_baseline_volume_percent(percent)

    state = context.state
    if state.playback_sink_idx is not None:
        set_sink_input_volume_percent(state.playback_sink_idx, percent)
    elif state.playback_process and state.playback_process.poll() is None:
        set_sink_volume_percent(state.playback_process.pid, percent)

    context.print(f"🔊 Громкость: {percent}%")


def _handle_volume(text: str, context: CommandContext) -> bool:
    normalized = text.strip().lower()

    m = _VOLUME_SET_RE.match(normalized)
    if m:
        parsed = _parse_volume_percent(m.group(1))
        if parsed is not None:
            _apply_volume(parsed, context)
        else:
            context.print("❌ Не понял уровень громкости. Пример: «поставь громкость на 40%».")
        return True

    current = load_baseline_volume_percent()
    if _VOLUME_UP_RE.match(normalized):
        _apply_volume(current + _STEP_PERCENT, context)
        return True
    if _VOLUME_DOWN_RE.match(normalized):
        _apply_volume(current - _STEP_PERCENT, context)
        return True

    return False


def register_volume_commands() -> None:
    register_command(is_volume_command, _handle_volume)
