"""
Состояние голосового ассистента — единый потокобезопасный объект.

Три режима:
  IDLE        — ждём wake word, микрофон слушает, STT работает
  LISTENING   — wake word распознан, копим текст команды
  PROCESSING  — текст стабилизировался, выполняем команду
"""

import enum
import subprocess
import threading
import time


class AssistantMode(enum.Enum):
    IDLE = "idle"
    LISTENING = "listening"
    PROCESSING = "processing"


class AssistantState:
    """
    Потокобезопасное состояние ассистента.

    Обращения идут из трёх контекстов:
      • аудио-колбэк  (sounddevice thread)  — STT, wake word, обновление текста
      • главный цикл   (main thread)         — duck/restore, таймауты, команды
      • фоновые потоки  (daemon threads)      — отслеживание ffplay, позиция
    """

    def __init__(self):
        self.lock = threading.RLock()

        # ── ядро ──
        self.mode: AssistantMode = AssistantMode.IDLE
        self.recognized_text: str = ""
        self.text_changed_at: float = 0.0
        self.cooldown_until: float = 0.0
        self.ducked: bool = False
        self.wake_input_dbfs: float = -60.0

        # ── воспроизведение ──
        self.playback_process: subprocess.Popen | None = None
        self.playback_sink_idx: int | None = None
        self.last_played_query: str | None = None
        self.last_played_track_id: int | str | None = None
        self.prev_played_query: str | None = None
        self.prev_played_track_id: int | str | None = None
        self.station_playlist: list[dict] = []
        self.station_playlist_pos: int = 0
        self.station_seed_track_id: int | str | None = None
        self.playback_position_sec: float = 0.0
        self.playback_segment_base_sec: float = 0.0
        self.playback_segment_started_at: float | None = None

        # сигнал аудио-колбэк → главный цикл: «wake word обнаружен»
        self.wake_detected = threading.Event()

    # ── Свойства ──

    @property
    def in_cooldown(self) -> bool:
        return time.monotonic() < self.cooldown_until

    @property
    def is_playing(self) -> bool:
        proc = self.playback_process
        return proc is not None and proc.poll() is None

    # ── Текст (из аудио-колбэка) ──

    def update_text(self, text: str) -> None:
        """Обновить распознанный текст; метка времени — только при изменении."""
        with self.lock:
            if text != self.recognized_text:
                self.recognized_text = text
                self.text_changed_at = time.monotonic()

    # ── Позиция воспроизведения ──

    def accumulate_position(self) -> None:
        """Пересчитать playback_position_sec по текущему сегменту."""
        with self.lock:
            if not self.is_playing or self.playback_segment_started_at is None:
                return
            self.playback_position_sec = (
                self.playback_segment_base_sec
                + (time.monotonic() - self.playback_segment_started_at)
            )

    def clear_playback_position(self) -> None:
        with self.lock:
            self.playback_position_sec = 0.0
            self.playback_segment_base_sec = 0.0
            self.playback_segment_started_at = None

    def mark_segment_started(self, base_sec: float) -> None:
        with self.lock:
            self.playback_segment_base_sec = base_sec
            self.playback_segment_started_at = time.monotonic()

    # ── Переходы режимов ──

    def begin_listening(self, text: str, wake_dbfs: float | None = None) -> None:
        """IDLE → LISTENING (wake word обнаружен в аудио-колбэке)."""
        with self.lock:
            self.mode = AssistantMode.LISTENING
            self.recognized_text = text
            self.text_changed_at = time.monotonic()
            if wake_dbfs is not None:
                self.wake_input_dbfs = float(wake_dbfs)

    def finish_command(self, cooldown: float) -> None:
        """PROCESSING → IDLE (команда обработана, ставим кулдаун)."""
        with self.lock:
            self.mode = AssistantMode.IDLE
            self.recognized_text = ""
            self.text_changed_at = 0.0
            self.cooldown_until = time.monotonic() + cooldown
