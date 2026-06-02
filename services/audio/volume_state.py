"""
Базовая громкость воспроизведения (PulseAudio sink-input) — хранится в локальном файле.
Все ffplay (музыка, сигнал активации) выравниваются на это значение; duck при wake word
считается от него же.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import settings

logger = logging.getLogger(__name__)


def volume_state_path() -> Path:
    return Path(getattr(settings, "SPEAKER_VOLUME_STATE_PATH"))


def load_baseline_volume_percent() -> int:
    """Целевая громкость 0–100 для ffplay (по умолчанию 100)."""
    path = volume_state_path()
    if not path.is_file():
        save_baseline_volume_percent(100)
        return 100
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        v = int(data.get("percent", 100))
        return max(0, min(100, v))
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return 100


def save_baseline_volume_percent(percent: int) -> None:
    """Сохранить базовую громкость (для будущих голосовых команд «тише/громче»)."""
    percent = max(0, min(100, int(percent)))
    path = volume_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"percent": percent}, ensure_ascii=False, indent=0) + "\n",
        encoding="utf-8",
    )
    logger.info("speaker baseline volume saved: %s%%", percent)
