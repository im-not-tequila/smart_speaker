"""
Управление громкостью через PulseAudio/PipeWire (pactl).

Базовая громкость из файла (.speaker_volume.json); duck при wake word = доля от базы.
Поддержка shell-пайплайнов (ffmpeg | ffplay): обход дерева дочерних PID через /proc.
Кэширование sink-input idx — ducking/restore за один вызов pactl.
"""

import os
import re
import subprocess
import threading
import time
from pathlib import Path

from services.audio.volume_state import load_baseline_volume_percent

_PACTL_ENV = {**os.environ, "LANG": "C", "LC_ALL": "C"}


# ── Поиск sink-input ─────────────────────────────────────────────


def _find_sink_input_for_pid(pid: int) -> int | None:
    """Индекс sink-input PulseAudio для процесса с данным PID."""
    try:
        result = subprocess.run(
            ["pactl", "list", "sink-inputs"],
            capture_output=True, text=True, timeout=2, env=_PACTL_ENV,
        )
        if result.returncode != 0:
            return None

        current_index = None
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if stripped.startswith("Sink Input #"):
                m = re.search(r"#(\d+)", stripped)
                current_index = int(m.group(1)) if m else None
            elif "application.process.id" in stripped and current_index is not None:
                parts = stripped.split("=", 1)
                if len(parts) == 2:
                    try:
                        if int(parts[1].strip().strip('"')) == pid:
                            return current_index
                    except ValueError:
                        pass
                current_index = None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def _collect_descendant_pids(pid: int) -> list[int]:
    """Все дочерние PID рекурсивно через /proc (для shell-пайплайнов)."""
    result: list[int] = []
    try:
        text = Path(f"/proc/{pid}/task/{pid}/children").read_text().strip()
        children = [int(p) for p in text.split() if p]
        result.extend(children)
        for cpid in children:
            result.extend(_collect_descendant_pids(cpid))
    except (OSError, ValueError):
        pass
    return result


def resolve_sink_input(proc: subprocess.Popen, timeout: float = 3.0) -> int | None:
    """
    Находит sink-input для процесса воспроизведения (прямой ffplay или
    shell-пайплайн ffmpeg|ffplay).  Ждёт до timeout секунд.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return None
        idx = _find_sink_input_for_pid(proc.pid)
        if idx is not None:
            return idx
        if getattr(proc, "_terminate_process_group", False):
            for cpid in _collect_descendant_pids(proc.pid):
                idx = _find_sink_input_for_pid(cpid)
                if idx is not None:
                    return idx
        time.sleep(0.12)
    return None


# ── Установка громкости ──────────────────────────────────────────


def _set_volume(sink_input_index: int, percent: int) -> bool:
    percent = max(0, min(100, int(percent)))
    try:
        subprocess.run(
            ["pactl", "set-sink-input-volume", str(sink_input_index), f"{percent}%"],
            capture_output=True, timeout=2,
        )
        return True
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


# ── Публичное API: быстрые операции по кэшированному idx ─────────


def duck_sink_input(idx: int, factor_percent: int) -> bool:
    """Приглушить sink-input: новая = round(база × factor_percent / 100)."""
    baseline = load_baseline_volume_percent()
    new_vol = max(1, min(100, round(baseline * factor_percent / 100.0)))
    return _set_volume(idx, new_vol)


def restore_sink_input(idx: int) -> bool:
    """Вернуть sink-input к базовой громкости из файла."""
    return _set_volume(idx, load_baseline_volume_percent())


def set_sink_input_volume_percent(idx: int, percent: int) -> bool:
    """Выставить конкретную громкость для sink-input по его индексу."""
    return _set_volume(idx, percent)


# ── Фоновые задачи ───────────────────────────────────────────────


def schedule_apply_baseline_volume(proc: subprocess.Popen) -> None:
    """Фон: дождаться sink-input → выставить базовую громкость (для activate sound и т.п.)."""

    def run():
        if proc.poll() is not None:
            return
        idx = resolve_sink_input(proc, timeout=2.0)
        if idx is not None:
            _set_volume(idx, load_baseline_volume_percent())

    threading.Thread(target=run, daemon=True).start()


def resolve_and_cache_sink_input(proc: subprocess.Popen, state) -> None:
    """
    Фон: найти sink-input для нового воспроизведения, выставить базовую
    громкость и сохранить индекс в state.playback_sink_idx.

    state — объект AssistantState (duck typing, без импорта).
    """

    def run():
        if proc.poll() is not None:
            return
        idx = resolve_sink_input(proc, timeout=3.0)
        if idx is None:
            return
        _set_volume(idx, load_baseline_volume_percent())
        if state.playback_process is proc:
            state.playback_sink_idx = idx

    threading.Thread(target=run, daemon=True).start()


# ── Совместимость: PID-ориентированные функции ───────────────────


def set_sink_volume_percent(pid: int, percent: int) -> bool:
    """Выставить громкость потока процесса pid (ждёт появления sink-input)."""
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        idx = _find_sink_input_for_pid(pid)
        if idx is not None:
            return _set_volume(idx, percent)
        time.sleep(0.1)
    return False


def duck_for_assistant(pid: int, factor_percent: int) -> bool:
    """Приглушение по PID (фоллбэк, если нет кэшированного idx)."""
    idx = _find_sink_input_for_pid(pid)
    if idx is None:
        return False
    return duck_sink_input(idx, factor_percent)


def restore_to_baseline_volume(pid: int) -> bool:
    """Восстановление по PID (фоллбэк)."""
    idx = _find_sink_input_for_pid(pid)
    if idx is None:
        return False
    return _set_volume(idx, load_baseline_volume_percent())
