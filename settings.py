"""
Конфигурация проекта smart_speaker.
"""

import json
import os
from pathlib import Path

from dotenv import load_dotenv

# Корень проекта
PROJECT_ROOT = Path(__file__).resolve().parent

load_dotenv(PROJECT_ROOT / ".env")
MODELS_DIR = PROJECT_ROOT / "models"
VOICE_COMMANDS_PATH = PROJECT_ROOT / "voice_commands.json"

DEFAULT_VOICE_COMMANDS = {
    "assistant_names": ["алёша", "алеша", "алексей"],
    "music": {
        "continue_regex": (
            r"^(продолжи|продолжить|продолжай|"
            r"продолжи\s+воспроизведение|играй\s+дальше)$"
        ),
        "next_regex": (
            r"^((?:включи\s+)?следующ(?:ий|ую)(?:\s+трек)?|"
            r"(?:включи\s+)?дальше(?:\s+трек)?|дальше|переключи\s+дальше)$"
        ),
        "previous_regex": (
            r"^((?:включи\s+)?предыдущ(?:ий|ую)(?:\s+трек)?|"
            r"(?:включи\s+)?назад(?:\s+трек)?|назад|переключи\s+назад)$"
        ),
        "stop_regex": (
            r"^(стоп|stop|стоп\s+музыку|пауза|паузу|"
            r"останови|останови\s+музыку|останови\s+песню|"
            r"выключи\s+музыку|поставь\s+на\s+паузу|"
            r"хватит|тишина|замолчи|замолкни)$"
        ),
        "play_patterns": [
            r"включи\s+песню\s+(.+)",
            r"поставь\s+песню\s+(.+)",
            r"играй\s+песню\s+(.+)",
            r"включи\s+(.+)",
            r"поставь\s+(.+)",
            r"играй\s+(.+)",
            r"найди\s+песню\s+(.+)",
            r"найди\s+(.+)",
            r"запусти\s+песню\s+(.+)",
            r"запусти\s+(.+)",
        ],
    },
    "volume": {
        "set_regex": r"^(?:поставь|установи|сделай)\s+(?:громкость|звук)\s+на\s+(.+)$",
        "up_regex": r"^(?:сделай\s+громче|увеличь\s+громкость|прибавь\s+громкость)$",
        "down_regex": r"^(?:сделай\s+тише|уменьши\s+громкость|убавь\s+громкость)$",
    },
}


def _deep_merge(default: dict, override: dict) -> dict:
    merged = dict(default)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _load_voice_commands_config() -> dict:
    try:
        if VOICE_COMMANDS_PATH.is_file():
            raw = json.loads(VOICE_COMMANDS_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return _deep_merge(DEFAULT_VOICE_COMMANDS, raw)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        pass
    return dict(DEFAULT_VOICE_COMMANDS)


VOICE_COMMANDS = _load_voice_commands_config()
_DEFAULT_VOICE_COMMANDS = DEFAULT_VOICE_COMMANDS  # backward compatibility

# === Голосовой ассистент ===
WAKE_WORDS = list(VOICE_COMMANDS.get("assistant_names", DEFAULT_VOICE_COMMANDS["assistant_names"]))
TEXT_STABLE_TIMEOUT = 3.0  # сек без изменений текста — команда завершена
# «Стоп»/пауза: короче ожидание, иначе позиция «продолжи» уезжает вперёд (музыка играет всё время STT)
STOP_COMMAND_STABLE_TIMEOUT = 0.65
# При «продолжи» начать немного раньше точки остановки
RESUME_REWIND_SECONDS = 5.0
COOLDOWN_AFTER_COMMAND = 1.5  # сек — игнорируем wake word (защита от двойного срабатывания)
# Доля от базовой громкости из SPEAKER_VOLUME_STATE при wake word во время музыки (72 → 0.72×)
DUCK_VOLUME_PERCENT = 60
# Адаптивный ducking по уровню микрофона (dBFS в момент wake word):
# при тихом голосе уменьшаем музыку сильнее, при громком — слабее.
ADAPTIVE_DUCK_ENABLED = True
ADAPTIVE_DUCK_DBFS_LOW = -50.0
ADAPTIVE_DUCK_DBFS_HIGH = -20.0
ADAPTIVE_DUCK_MIN_PERCENT = 18
ADAPTIVE_DUCK_MAX_PERCENT = 65
# JSON: {"percent": 100} — базовая громкость всех ffplay; duck считается от неё
SPEAKER_VOLUME_STATE_PATH = PROJECT_ROOT / ".speaker_volume.json"
# Короткий сигнал «ассистент услышал wake word и слушает»
ACTIVATE_SOUND_PATH = PROJECT_ROOT / "assests" / "activate.wav"
AUDIO_SAMPLE_RATE = 16000
AUDIO_CHUNK_MS = 100  # размер чанка в мс для callback

# === STT (sherpa-onnx) ===
STT_MODEL_PATH = MODELS_DIR / "sherpa-onnx-t-one-ru"
STT_NUM_THREADS = 2
STT_PROVIDER = "cpu"

# === Gemini (нормализация запросов перед поиском) ===
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL_NAME = "gemini-2.5-flash"

# === SoundCloud API (личные токены — только из .env) ===
# Формат OAuth-токена: «2-…» без префикса «OAuth »
SOUNDCLOUD_OAUTH_TOKEN = os.getenv("SOUNDCLOUD_OAUTH_TOKEN", "")
SOUNDCLOUD_CLIENT_ID = os.getenv("SOUNDCLOUD_CLIENT_ID", "")
SOUNDCLOUD_USER_ID = os.getenv("SOUNDCLOUD_USER_ID", "")
SOUNDCLOUD_SC_A_ID = os.getenv("SOUNDCLOUD_SC_A_ID", "")
SOUNDCLOUD_APP_VERSION = os.getenv("SOUNDCLOUD_APP_VERSION", "1759936298")
SOUNDCLOUD_APP_LOCALE = os.getenv("SOUNDCLOUD_APP_LOCALE", "en")

# === SoundCloud: локальный кэш MP3 по track id (LRU по суммарному размеру) ===
SOUNDCLOUD_CACHE_DIR = PROJECT_ROOT / ".soundcloud_cache"
# 0 — кэш отключён (стрим по URL); иначе макс. суммарный размер файлов в каталоге
SOUNDCLOUD_CACHE_MAX_BYTES = 500 * 1024 * 1024
# Сколько треков заранее забирать из station/related после заказанного трека
SOUNDCLOUD_STATION_PREFETCH_LIMIT = 30
