"""
Голосовой ассистент — основной цикл, wake word, распознавание, выполнение команд.

Архитектура:
  • Аудио-колбэк (sounddevice thread) — ТОЛЬКО подаёт данные в STT и
    детектирует wake word / обновляет текст.  Никаких subprocess.
  • Главный цикл (main thread) — обрабатывает события wake_detected,
    управляет громкостью (duck / restore), определяет стабильность текста,
    вызывает команды.
  • Фоновые потоки — отслеживание ffplay, позиция воспроизведения,
    кэширование sink-input.
"""

import subprocess
import time
import math

import sounddevice as sd

from core.state import AssistantMode, AssistantState
from core.text_utils import text_after_wake_word
from settings import (
    ADAPTIVE_DUCK_DBFS_HIGH,
    ADAPTIVE_DUCK_DBFS_LOW,
    ADAPTIVE_DUCK_ENABLED,
    ADAPTIVE_DUCK_MAX_PERCENT,
    ADAPTIVE_DUCK_MIN_PERCENT,
    ACTIVATE_SOUND_PATH,
    AUDIO_CHUNK_MS,
    AUDIO_SAMPLE_RATE,
    COOLDOWN_AFTER_COMMAND,
    DUCK_VOLUME_PERCENT,
    STOP_COMMAND_STABLE_TIMEOUT,
    TEXT_STABLE_TIMEOUT,
    WAKE_WORDS,
)
from services.audio.ducking import (
    duck_sink_input,
    restore_sink_input,
    schedule_apply_baseline_volume,
)
from services.soundcloud.client import SoundcloudClient, terminate_playback_process
from services.voice.commands import CommandContext, handle_command, init_commands
from services.voice.commands.music import is_stop_command
from services.voice.recognizer import create_recognizer

_WAKE_WORDS_LOWER = [w.lower() for w in WAKE_WORDS]


def _audio_dbfs(audio) -> float:
    """
    Примерная оценка уровня входа в dBFS для текущего чанка микрофона.
    0 dBFS — максимум, более тихий звук: отрицательные значения.
    """
    if len(audio) == 0:
        return -80.0
    rms = float((audio * audio).mean()) ** 0.5
    if rms <= 1e-9:
        return -80.0
    return 20.0 * math.log10(rms)


class VoiceAssistant:
    """Голосовой ассистент с wake word и выполнением команд."""

    def __init__(self):
        self.recognizer = create_recognizer()
        self.soundcloud = SoundcloudClient()
        init_commands()

        self.sample_rate = AUDIO_SAMPLE_RATE
        self.chunk_size = int(AUDIO_CHUNK_MS / 1000 * self.sample_rate)

        self.state = AssistantState()
        self._stream = self.recognizer.create_stream()

    # ── Воспроизведение ──────────────────────────────────────────

    def _stop_playback(self) -> None:
        """Останавливает воспроизведение (финальная очистка при выходе)."""
        proc = self.state.playback_process
        if proc and proc.poll() is None:
            terminate_playback_process(proc)
        self.state.playback_process = None
        self.state.playback_sink_idx = None

    # ── Activate sound ───────────────────────────────────────────

    def _play_activate_sound(self) -> None:
        """Короткий сигнал «ассистент слушает»."""
        path = ACTIVATE_SOUND_PATH
        if not path.is_file():
            return
        proc = subprocess.Popen(
            ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        schedule_apply_baseline_volume(proc)

    # ── Ducking ──────────────────────────────────────────────────

    def _duck_playback(self) -> None:
        """Приглушить музыку (вызывается из главного цикла, НЕ из колбэка)."""
        if not self.state.is_playing:
            return
        idx = self.state.playback_sink_idx
        factor = DUCK_VOLUME_PERCENT
        if ADAPTIVE_DUCK_ENABLED:
            dbfs = self.state.wake_input_dbfs
            low = float(ADAPTIVE_DUCK_DBFS_LOW)
            high = float(ADAPTIVE_DUCK_DBFS_HIGH)
            min_p = int(ADAPTIVE_DUCK_MIN_PERCENT)
            max_p = int(ADAPTIVE_DUCK_MAX_PERCENT)
            if high <= low:
                high = low + 1.0
            t = (dbfs - low) / (high - low)
            t = max(0.0, min(1.0, t))
            factor = round(min_p + (max_p - min_p) * t)
        if idx is not None and duck_sink_input(idx, factor):
            self.state.ducked = True

    def _restore_playback_volume(self) -> None:
        """Вернуть громкость к базовой после обработки команды."""
        if not self.state.ducked:
            return
        idx = self.state.playback_sink_idx
        if idx is not None and self.state.is_playing:
            restore_sink_input(idx)
        self.state.ducked = False

    # ── Аудио-колбэк ─────────────────────────────────────────────
    #    ДОЛЖЕН БЫТЬ БЫСТРЫМ.  Никаких subprocess, IO, блокировок.

    def _make_callback(self):
        recognizer = self.recognizer
        state = self.state

        def callback(indata, frames, time_info, status):
            if status:
                print(status)

            stream = self._stream
            audio = indata.flatten()
            stream.accept_waveform(self.sample_rate, audio)

            while recognizer.is_ready(stream):
                recognizer.decode_stream(stream)

            result = recognizer.get_result(stream)
            if not result:
                return

            text = result.strip()

            if state.mode == AssistantMode.IDLE:
                if state.in_cooldown:
                    return
                text_lower = text.lower()
                if any(ww in text_lower for ww in _WAKE_WORDS_LOWER):
                    state.begin_listening(text, wake_dbfs=_audio_dbfs(audio))
                    state.wake_detected.set()
                    print(f"\rСлушаю... {text}", end="", flush=True)

            elif state.mode == AssistantMode.LISTENING:
                state.update_text(text)
                print(f"\rСлушаю... {text}", end="", flush=True)

        return callback

    # ── Обработка команды ────────────────────────────────────────

    def _process_command(self, text: str) -> None:
        ctx = CommandContext(
            state=self.state,
            soundcloud=self.soundcloud,
            print=print,
        )

        if text:
            print(f"\n\n>>> {text}\n")
            handle_command(text, ctx)
        else:
            print("Не расслышал команду — повторите или скажите «продолжи».")

        self._restore_playback_volume()

    # ── Главный цикл ─────────────────────────────────────────────

    def run(self) -> None:
        print("🎵 Голосовой музыкальный ассистент")
        print(f"   Скажите «{', '.join(WAKE_WORDS).capitalize()} включи песню <название>»")
        print("   Остановить: «стоп», «пауза», «выключи музыку», «поставь на паузу» и т.п.")
        print("   Продолжить с места остановки: «продолжи», «дальше»")
        print("   Переключение во время музыки: «включи следующий трек», «включи предыдущий трек»")
        print("   Громкость: «сделай тише/громче», «поставь громкость на 40%»")
        print("   Пример: «Алёша включи песню атл я забил»")
        print(
            f"   Текст не меняется {TEXT_STABLE_TIMEOUT} сек — выполнение команды "
            f"(«стоп»/пауза — около {STOP_COMMAND_STABLE_TIMEOUT} с). Ctrl+C для выхода.\n"
        )

        try:
            with sd.InputStream(
                samplerate=self.sample_rate,
                channels=1,
                callback=self._make_callback(),
                blocksize=self.chunk_size,
            ):
                while True:
                    sd.sleep(50)

                    # 1. Wake word обнаружен → duck + activate
                    if self.state.wake_detected.is_set():
                        self.state.wake_detected.clear()
                        self._duck_playback()
                        self._play_activate_sound()

                    # 2. Проверяем стабильность текста в режиме LISTENING
                    if self.state.mode != AssistantMode.LISTENING:
                        continue

                    text = self.state.recognized_text
                    if not text:
                        continue

                    elapsed = time.monotonic() - self.state.text_changed_at
                    command_text = text_after_wake_word(text, WAKE_WORDS)
                    stable_for = (
                        STOP_COMMAND_STABLE_TIMEOUT
                        if is_stop_command(command_text)
                        else TEXT_STABLE_TIMEOUT
                    )

                    if elapsed >= stable_for:
                        self.state.mode = AssistantMode.PROCESSING
                        self._stream = self.recognizer.create_stream()

                        self._process_command(command_text)

                        self.state.finish_command(COOLDOWN_AFTER_COMMAND)
                        print(f"\nСкажите «{', '.join(WAKE_WORDS).capitalize()}»...")

        except KeyboardInterrupt:
            print("\nВыход.")
        finally:
            self._stop_playback()
