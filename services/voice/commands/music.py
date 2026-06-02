"""
Команды воспроизведения музыки с SoundCloud.
"""

import re
import threading
import time

import settings
from services.ai.query_normalizer import normalize_query
from services.audio.ducking import resolve_and_cache_sink_input
from services.soundcloud.client import SoundcloudClient, terminate_playback_process
from services.voice.commands.base import CommandContext, register_command


# ── Утилиты позиции воспроизведения ──────────────────────────────


def _schedule_playback_position_poll(proc, state) -> None:
    """Фоновый поток: периодически обновляет playback_position_sec."""

    def loop():
        while True:
            time.sleep(0.35)
            if state.playback_process is not proc or proc.poll() is not None:
                return
            state.accumulate_position()

    threading.Thread(target=loop, daemon=True).start()


def _schedule_playback_done_thread(proc, state, context: CommandContext) -> None:
    """Фоновый поток: ждёт завершения ffplay и обновляет состояние."""

    def on_done():
        proc.wait()
        with state.lock:
            if state.playback_process is not proc:
                return
            state.playback_process = None
            state.playback_sink_idx = None
            seg_start = state.playback_segment_started_at
            elapsed = (time.monotonic() - seg_start) if seg_start is not None else 0.0
            base_sec = state.playback_segment_base_sec

        suspicious = proc.returncode == 0 and base_sec > 2.0 and elapsed < 0.75

        if proc.returncode == 0 and not suspicious:
            if _try_autoplay_next_track(context):
                return
            state.clear_playback_position()
            context.print("\n✅ Готово. Что ещё включить?")
        elif suspicious:
            with state.lock:
                state.playback_segment_base_sec = 0.0
                state.playback_segment_started_at = None
            context.print(
                "\n⚠️ Возобновление с этой позиции не сработало. "
                "Повторите «продолжи» или включите трек снова."
            )
        else:
            context.print(f"\n❌ Воспроизведение завершилось с кодом {proc.returncode}.")

    threading.Thread(target=on_done, daemon=True).start()


def _reset_station_playlist(state, seed_track_id: int | str | None = None) -> None:
    with state.lock:
        state.station_seed_track_id = seed_track_id
        state.station_playlist = []
        state.station_playlist_pos = 0


def _start_station_prefetch_async(context: CommandContext, seed_track_id: int | str) -> None:
    """Фоном подгружает station/related список для следующего воспроизведения без задержек."""
    soundcloud: SoundcloudClient = context.soundcloud
    state = context.state
    limit = max(1, int(getattr(settings, "SOUNDCLOUD_STATION_PREFETCH_LIMIT", 30)))

    def run() -> None:
        try:
            related = soundcloud.related_tracks(seed_track_id, limit=limit)
        except Exception as e:
            context.print(f"\n⚠️ Не удалось подготовить station-плейлист: {e}")
            return
        if not related:
            return
        with state.lock:
            if state.station_seed_track_id != seed_track_id:
                return
            state.station_playlist = related
            state.station_playlist_pos = 0
        context.print(f"\n📻 Station-плейлист подготовлен: {len(related)} треков")

    threading.Thread(target=run, daemon=True).start()


def _build_station_playlist_sync(context: CommandContext, seed_track_id: int | str) -> bool:
    """Синхронно обновляет station-плейлист для указанного трека."""
    soundcloud: SoundcloudClient = context.soundcloud
    state = context.state
    limit = max(1, int(getattr(settings, "SOUNDCLOUD_STATION_PREFETCH_LIMIT", 30)))
    related = soundcloud.related_tracks(seed_track_id, limit=limit)
    with state.lock:
        state.station_seed_track_id = seed_track_id
        state.station_playlist = related
        state.station_playlist_pos = 0
    return bool(related)


def _play_next_from_station_playlist(context: CommandContext) -> bool:
    """Включает следующий трек из заранее подготовленного station-плейлиста."""
    state = context.state
    soundcloud: SoundcloudClient = context.soundcloud

    while True:
        with state.lock:
            pos = state.station_playlist_pos
            if pos >= len(state.station_playlist):
                return False
            track = state.station_playlist[pos]

        try:
            proc, tid, query = soundcloud.start_track_playback(track, start_seconds=0.0)
        except Exception as e:
            with state.lock:
                state.station_playlist_pos = pos + 1
            context.print(f"\n⚠️ Пропускаю трек из station-плейлиста: {e}")
            continue

        with state.lock:
            state.station_playlist_pos = pos + 1
        _start_playback(proc, state, context, track_id=tid, query=query, base_sec=0.0)
        return True


def _try_autoplay_next_track(context: CommandContext) -> bool:
    """Автозапуск следующего трека после естественного завершения текущего."""
    context.print("\n⏭️ Трек закончился — включаю следующий.")
    if _play_next_from_station_playlist(context):
        return True

    current_tid = context.state.last_played_track_id
    if current_tid is None:
        return False
    try:
        if _build_station_playlist_sync(context, current_tid):
            return _play_next_from_station_playlist(context)
    except Exception as e:
        context.print(f"\n⚠️ Не удалось обновить station-плейлист: {e}")
    return False


def _stop_current_playback(state) -> None:
    """Останавливает текущий ffplay и сбрасывает связанное состояние."""
    proc = state.playback_process
    if proc and proc.poll() is None:
        terminate_playback_process(proc)
    state.playback_process = None
    state.playback_sink_idx = None


def _start_playback(
    proc,
    state,
    context,
    *,
    track_id,
    query: str | None = None,
    base_sec: float = 0.0,
) -> None:
    """Общая инициализация после запуска нового ffplay."""
    if state.last_played_track_id is not None and state.last_played_track_id != track_id:
        state.prev_played_track_id = state.last_played_track_id
        state.prev_played_query = state.last_played_query
    state.playback_process = proc
    state.last_played_track_id = track_id
    if query is not None:
        state.last_played_query = query
    state.mark_segment_started(base_sec)
    resolve_and_cache_sink_input(proc, state)
    _schedule_playback_position_poll(proc, state)
    _schedule_playback_done_thread(proc, state, context)


# ── «Продолжи» ──────────────────────────────────────────────────


_MUSIC_CFG = settings.VOICE_COMMANDS.get("music", {})
_CONTINUE_RE = re.compile(
    str(_MUSIC_CFG.get("continue_regex", settings.DEFAULT_VOICE_COMMANDS["music"]["continue_regex"])),
    re.IGNORECASE,
)
_NEXT_RE = re.compile(
    str(_MUSIC_CFG.get("next_regex", settings.DEFAULT_VOICE_COMMANDS["music"]["next_regex"])),
    re.IGNORECASE,
)
_PREVIOUS_RE = re.compile(
    str(_MUSIC_CFG.get("previous_regex", settings.DEFAULT_VOICE_COMMANDS["music"]["previous_regex"])),
    re.IGNORECASE,
)


def is_continue_command(text: str) -> bool:
    return bool(text.strip() and _CONTINUE_RE.match(text.strip()))


def is_next_command(text: str) -> bool:
    return bool(text.strip() and _NEXT_RE.match(text.strip()))


def is_previous_command(text: str) -> bool:
    return bool(text.strip() and _PREVIOUS_RE.match(text.strip()))


def _handle_continue_music(_text: str, context: CommandContext) -> bool:
    if not _handle_resume_playback(context):
        context.print("❌ Нет недавнего трека — сначала включите что-нибудь")
    return True


def _handle_next_music(_text: str, context: CommandContext) -> bool:
    state = context.state
    if not state.is_playing:
        context.print("⏭️ Команда «следующий трек» работает только во время воспроизведения")
        return True

    try:
        context.print("⏭️ Переключаю на следующий трек")
        _stop_current_playback(state)
        if _play_next_from_station_playlist(context):
            return True
        current_tid = state.last_played_track_id
        if current_tid is None:
            context.print("❌ Не удалось определить текущий трек")
            return True
        if _build_station_playlist_sync(context, current_tid) and _play_next_from_station_playlist(context):
            return True
        context.print("❌ В station-плейлисте нет следующего трека")
    except Exception as e:
        context.print(f"❌ Не удалось включить следующий трек: {e}")
    return True


def _handle_previous_music(_text: str, context: CommandContext) -> bool:
    state = context.state
    if not state.is_playing:
        context.print("⏮️ Команда «предыдущий трек» работает только во время воспроизведения")
        return True

    prev_tid = state.prev_played_track_id
    prev_query = state.prev_played_query
    if prev_tid is None or not prev_query:
        context.print("❌ Нет предыдущего трека в истории")
        return True

    try:
        soundcloud: SoundcloudClient = context.soundcloud
        context.print("⏮️ Переключаю на предыдущий трек")
        proc, tid = soundcloud.resume_playback(prev_tid, prev_query, 0.0)
        _stop_current_playback(state)
        _reset_station_playlist(state, seed_track_id=tid)
        _start_playback(proc, state, context, track_id=tid, query=prev_query, base_sec=0.0)
        _start_station_prefetch_async(context, tid)
    except Exception as e:
        context.print(f"❌ Не удалось включить предыдущий трек: {e}")
    return True


# ── «Стоп» / «Пауза» ───────────────────────────────────────────


_STOP_RE = re.compile(
    str(_MUSIC_CFG.get("stop_regex", settings.DEFAULT_VOICE_COMMANDS["music"]["stop_regex"])),
    re.IGNORECASE,
)


def is_stop_command(text: str) -> bool:
    return bool(text.strip() and _STOP_RE.match(text.strip()))


def _handle_stop_music(_text: str, context: CommandContext) -> bool:
    state = context.state
    was_playing = state.is_playing
    if was_playing:
        state.accumulate_position()
    _stop_current_playback(state)
    context.print("⏹️ Воспроизведение остановлено" if was_playing else "Сейчас ничего не играет")
    return True


# ── «Включи» / «Поставь» / «Играй» и т.д. ──────────────────────


_DEFAULT_PLAY_PATTERNS = settings.DEFAULT_VOICE_COMMANDS["music"]["play_patterns"]
_PLAY_PATTERNS = _MUSIC_CFG.get("play_patterns", _DEFAULT_PLAY_PATTERNS)
MUSIC_PATTERNS = [
    re.compile(str(pattern), re.IGNORECASE)
    for pattern in (_PLAY_PATTERNS if isinstance(_PLAY_PATTERNS, list) else _DEFAULT_PLAY_PATTERNS)
]


def is_music_command(text: str) -> bool:
    text = text.strip().lower()
    if not text:
        return False
    for pattern in MUSIC_PATTERNS:
        m = pattern.search(text)
        if m and m.group(1).strip():
            return True
    return False


def extract_song_query(text: str) -> str | None:
    text = text.strip().lower()
    if not text:
        return None
    for pattern in MUSIC_PATTERNS:
        m = pattern.search(text)
        if m:
            query = m.group(1).strip()
            if query:
                return query
    return text or None


def _handle_play_music(text: str, context: CommandContext) -> bool:
    query = extract_song_query(text)
    if not query:
        context.print("❌ Не понял, какую песню включить. Пример: «включи песню атл я забил»")
        return True

    soundcloud: SoundcloudClient = context.soundcloud
    state = context.state

    try:
        state.clear_playback_position()
        context.print(f"🔍 Ищу: {query}")
        normalized = normalize_query(query)
        if normalized != query:
            context.print(f"🔎 → {normalized}")
        _stop_current_playback(state)
        proc, tid = soundcloud.search_and_start_playback(normalized, start_seconds=0.0)
        _reset_station_playlist(state, seed_track_id=tid)
        _start_playback(proc, state, context, track_id=tid, query=normalized, base_sec=0.0)
        _start_station_prefetch_async(context, tid)
    except ValueError as e:
        context.print(f"❌ {e}")
    except Exception as e:
        context.print(f"❌ Ошибка: {e}")

    return True


# ── Возобновление ────────────────────────────────────────────────


def _handle_resume_playback(context: CommandContext) -> bool:
    state = context.state
    query = state.last_played_query
    if not query:
        return False

    soundcloud: SoundcloudClient = context.soundcloud
    rewind_sec = max(0.0, float(getattr(settings, "RESUME_REWIND_SECONDS", 5.0)))
    start_sec = max(0.0, state.playback_position_sec - rewind_sec)

    try:
        context.print(
            "▶️ Продолжаю воспроизведение"
            + (f" с {start_sec:.0f} с" if start_sec > 0.5 else "")
        )
        tid = state.last_played_track_id
        _stop_current_playback(state)
        proc, new_tid = soundcloud.resume_playback(tid, query, start_sec)
        _start_playback(proc, state, context, track_id=new_tid, query=query, base_sec=start_sec)
        return True
    except Exception as e:
        context.print(f"❌ Не удалось возобновить: {e}")
        return True


# ── Регистрация ──────────────────────────────────────────────────


def register_music_commands() -> None:
    register_command(is_continue_command, _handle_continue_music)
    register_command(is_next_command, _handle_next_music)
    register_command(is_previous_command, _handle_previous_music)
    register_command(is_stop_command, _handle_stop_music)
    register_command(is_music_command, _handle_play_music)


def try_resume_playback(context: CommandContext) -> bool:
    return _handle_resume_playback(context)
