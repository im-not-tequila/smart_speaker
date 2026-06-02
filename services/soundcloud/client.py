import logging
import os
import shlex
import signal
import subprocess
from pathlib import Path

from requests import Session
from urllib.parse import urlparse

import settings
from services.soundcloud.track_cache import TrackCache

logger = logging.getLogger(__name__)


def terminate_playback_process(proc: subprocess.Popen | None) -> None:
    """Завершает ffplay или shell-пайп ffmpeg|ffplay (см. _popen_ffplay)."""
    if not proc or proc.poll() is not None:
        return
    if getattr(proc, "_terminate_process_group", False):
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            proc.terminate()
    else:
        proc.terminate()


def _is_http_url(s: str) -> bool:
    return s.startswith(("http://", "https://"))


def _popen_ffplay(play_target: str, start_seconds: float) -> subprocess.Popen:
    """
    Локальный файл: ffplay -ss. HTTP(S) с ненулевым seek: ffmpeg с -ss и wav в ffplay,
    иначе ffplay -ss по URL (часто HLS) сразу завершается без звука.
    """
    if start_seconds <= 0.05 or not _is_http_url(play_target):
        cmd = ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"]
        if start_seconds > 0.05:
            cmd.extend(["-ss", f"{start_seconds:.3f}"])
        cmd.append(play_target)
        return subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

    safe = shlex.quote(play_target)
    # -ss после -i: для HLS/редиректов иначе часто мгновенный EOF и тишина (код 0).
    shell_cmd = (
        f"ffmpeg -hide_banner -loglevel error -i {safe} -ss {start_seconds:.3f} "
        "-vn -acodec pcm_s16le -ar 44100 -ac 2 -f wav - "
        "| ffplay -nodisp -autoexit -loglevel quiet -i -"
    )
    proc = subprocess.Popen(
        ["/bin/sh", "-c", shell_cmd],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    proc._terminate_process_group = True  # type: ignore[attr-defined]
    return proc


def _is_hls_playlist_chunk(chunk: bytes) -> bool:
    """SoundCloud часто отдаёт не сырой mp3, а M3U8 — такой поток нельзя сохранить как один .mp3."""
    if not chunk:
        return False
    s = chunk.lstrip(b"\xef\xbb\xbf")
    return s.startswith(b"#EXTM3U") or s.startswith(b"#EXT-X-")


def _pick_best_transcoding(transcodings: list[dict]) -> dict:
    """
    Выбирает лучший transcoding для кэша/seek:
    1) progressive mp3
    2) любой progressive
    3) mp3 (в т.ч. hls)
    4) первый доступный
    """
    if not transcodings:
        raise ValueError("Пустой список transcodings")

    def _is_progressive(tc: dict) -> bool:
        return (tc.get("format", {}) or {}).get("protocol") == "progressive"

    def _is_mp3(tc: dict) -> bool:
        preset = str(tc.get("preset", "")).lower()
        mime = str((tc.get("format", {}) or {}).get("mime_type", "")).lower()
        return "mp3" in preset or "audio/mpeg" in mime

    progressive_mp3 = next(
        (tc for tc in transcodings if _is_progressive(tc) and _is_mp3(tc)),
        None,
    )
    if progressive_mp3 is not None:
        return progressive_mp3

    progressive_any = next((tc for tc in transcodings if _is_progressive(tc)), None)
    if progressive_any is not None:
        return progressive_any

    mp3_any = next((tc for tc in transcodings if _is_mp3(tc)), None)
    if mp3_any is not None:
        return mp3_any

    return transcodings[0]


class SoundcloudClient:
    def __init__(self):
        self.user_agent = "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:143.0) Gecko/20100101 Firefox/143.0"
        self.session = self._make_sessions()
        max_b = int(getattr(settings, "SOUNDCLOUD_CACHE_MAX_BYTES", 0) or 0)
        cache_dir = getattr(settings, "SOUNDCLOUD_CACHE_DIR", None)
        self._cache: TrackCache | None = None
        if max_b > 0 and cache_dir is not None:
            self._cache = TrackCache(Path(cache_dir), max_b)

    def _make_sessions(self):
        session = Session()
        session.headers.update(self._make_headers())

        return session

    def _make_headers(self):
        headers = {
            "Host": "api-v2.soundcloud.com",
            "User-Agent": self.user_agent,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "ru-RU,ru;q=0.8,en-US;q=0.5,en;q=0.3",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Referer": "https://soundcloud.com/",
            "Origin": "https://soundcloud.com",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
            "Connection": "keep-alive",
        }
        token = settings.SOUNDCLOUD_OAUTH_TOKEN.strip()
        if token:
            headers["Authorization"] = (
                token if token.startswith("OAuth ") else f"OAuth {token}"
            )
        return headers

    def _api_params(self, **extra: str) -> dict[str, str]:
        params = {
            "client_id": settings.SOUNDCLOUD_CLIENT_ID,
            "app_version": settings.SOUNDCLOUD_APP_VERSION,
            "app_locale": settings.SOUNDCLOUD_APP_LOCALE,
        }
        params.update(extra)
        return params

    def search(self, q: str):
        url = "https://api-v2.soundcloud.com/search"

        params = self._api_params(
            q=f"{q}",
            sc_a_id=settings.SOUNDCLOUD_SC_A_ID,
            facet="model",
            user_id=settings.SOUNDCLOUD_USER_ID,
            limit="20",
            offset="0",
            linked_partitioning="1",
        )

        response = self.session.get(url, params=params)

        return response.json()

    def _resolve_media_url(self, transcoding_url: str, track_authorization: str) -> str:
        r = self.session.get(
            f"{transcoding_url}?track_authorization={track_authorization}"
        )
        r.raise_for_status()
        return r.json()["url"]

    def _stream_progressive_audio_to_path(self, media_url: str, dest: Path) -> bool:
        """
        Сохраняет поток в dest, если это не HLS-плейлист.
        Возвращает True при успешной записи, False если URL — M3U8 (нужен ffplay по URL).
        """
        parsed = urlparse(media_url)
        media_host = parsed.netloc
        download_headers = {
            "User-Agent": self.user_agent,
            "Accept": "*/*",
            "Referer": "https://soundcloud.com/",
            "Range": "bytes=0-",
            "Host": media_host,
            "Connection": "keep-alive",
        }
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_suffix(dest.suffix + ".part")
        try:
            with self.session.get(
                media_url, headers=download_headers, stream=True
            ) as audio:
                audio.raise_for_status()
                it = audio.iter_content(chunk_size=8192)
                first = next(it, b"")
                if _is_hls_playlist_chunk(first):
                    return False
                with open(part, "wb") as f:
                    if first:
                        f.write(first)
                    for chunk in it:
                        if chunk:
                            f.write(chunk)
            part.replace(dest)
            return True
        finally:
            if part.is_file():
                try:
                    part.unlink()
                except OSError:
                    pass

    def download(
        self,
        track_id: int | str,
        transcoding_url: str,
        track_authorization: str,
        path: str | Path | None = None,
    ) -> Path:
        """
        Скачивает трек. При включённом кэше (SOUNDCLOUD_CACHE_MAX_BYTES > 0) возвращает
        путь из кэша по track id, иначе пишет в path или track.mp3.
        """
        sid = str(track_id)
        if self._cache is not None:
            hit = self._cache.get_cached_path(sid)
            if hit is not None:
                return hit
            out = self._cache.file_path_for(sid)
            media_url = self._resolve_media_url(transcoding_url, track_authorization)
            logger.info("soundcloud cache miss track_id=%s — downloading", sid)
            if not self._stream_progressive_audio_to_path(media_url, out):
                raise ValueError(
                    "Трек отдаётся как HLS (M3U8), один файл .mp3 из потока не получить"
                )
            self._cache.register_download(sid, out)
            return out

        out = Path(path) if path is not None else Path("track.mp3")
        media_url = self._resolve_media_url(transcoding_url, track_authorization)
        if not self._stream_progressive_audio_to_path(media_url, out):
            raise ValueError(
                "Трек отдаётся как HLS (M3U8), скачайте через воспроизведение по URL"
            )
        logger.info("soundcloud downloaded (no cache) path=%s", out)
        return out

    def _cached_file_or_none(self, track_id: int | str, media_url: str) -> Path | None:
        """Путь в кэше или None — для HLS кэш не используем, ffplay играет по media_url."""
        assert self._cache is not None
        sid = str(track_id)
        hit = self._cache.get_cached_path(sid)
        if hit is not None:
            return hit
        out = self._cache.file_path_for(sid)
        logger.info("soundcloud cache miss track_id=%s — downloading", sid)
        if not self._stream_progressive_audio_to_path(media_url, out):
            logger.info(
                "soundcloud track_id=%s: HLS stream — playback by URL (skip cache)",
                sid,
            )
            return None
        self._cache.register_download(sid, out)
        return out

    def search_tracks(self, q: str) -> list[dict]:
        """Поиск треков, возвращает только объекты kind='track'."""
        data = self.search(q)
        return [item for item in data.get("collection", []) if item.get("kind") == "track"]

    def related_tracks(self, track_id: int | str, limit: int = 20) -> list[dict]:
        """Рекомендации после трека (логика станции SoundCloud)."""
        url = f"https://api-v2.soundcloud.com/tracks/{track_id}/related"
        params = self._api_params(
            limit=str(limit),
            offset="0",
            linked_partitioning="1",
        )
        response = self.session.get(url, params=params)
        response.raise_for_status()
        data = response.json()
        return [item for item in data.get("collection", []) if item.get("kind") == "track"]

    def _start_track_playback(
        self, track: dict, start_seconds: float = 0.0
    ) -> tuple[subprocess.Popen, int | str]:
        transcodings = track.get("media", {}).get("transcodings", [])
        if not transcodings:
            raise ValueError(f"Трек «{track.get('title')}» недоступен для воспроизведения")

        tc = _pick_best_transcoding(transcodings)
        transcoding_url = tc["url"]
        track_auth = track.get("track_authorization", "")
        if not track_auth:
            raise ValueError("Нет track_authorization для трека")

        tid = track.get("id")
        if tid is None:
            raise ValueError("У трека нет id в ответе API")

        print(f"Играю: {track['title']} — {track.get('user', {}).get('username', '?')}")

        media_url = self._resolve_media_url(transcoding_url, track_auth)
        if self._cache is not None:
            cached = self._cached_file_or_none(tid, media_url)
            play_target = str(cached) if cached is not None else media_url
        else:
            play_target = media_url

        return _popen_ffplay(play_target, start_seconds), tid

    @staticmethod
    def track_query(track: dict) -> str:
        """Строка запроса/подписи для «продолжи» и истории треков."""
        title = str(track.get("title", "")).strip()
        artist = str((track.get("user", {}) or {}).get("username", "")).strip()
        return f"{artist} {title}".strip() or title

    def start_related_playback(
        self, track_id: int | str
    ) -> tuple[subprocess.Popen, int | str, str]:
        """
        Запускает первый рекомендованный трек «после текущего».
        Возвращает (proc, next_track_id, next_query_for_resume).
        """
        related = self.related_tracks(track_id, limit=20)
        if not related:
            raise ValueError("Нет рекомендаций для следующего трека")
        next_track = related[0]
        proc, next_tid = self._start_track_playback(next_track, start_seconds=0.0)
        next_query = self.track_query(next_track) or str(next_tid)
        return proc, next_tid, next_query

    def start_track_playback(
        self, track: dict, start_seconds: float = 0.0
    ) -> tuple[subprocess.Popen, int | str, str]:
        """Запускает воспроизведение переданного трека (объект API)."""
        proc, tid = self._start_track_playback(track, start_seconds=start_seconds)
        query = self.track_query(track) or str(tid)
        return proc, tid, query

    def search_and_start_playback(
        self, query: str, start_seconds: float = 0.0
    ) -> tuple[subprocess.Popen, int | str]:
        """
        Ищет трек и запускает воспроизведение в фоне.
        Возвращает (процесс ffplay или shell-пайп, id трека для кэша / «продолжи»).
        start_seconds — смещение от начала (после паузы / «продолжи»).
        """
        tracks = self.search_tracks(query)
        if not tracks:
            raise ValueError(f"По запросу «{query}» треки не найдены")
        return self._start_track_playback(tracks[0], start_seconds=start_seconds)

    def resume_playback(
        self,
        track_id: int | str | None,
        query: str,
        start_seconds: float,
    ) -> tuple[subprocess.Popen, int | str]:
        """
        «Продолжи»: если трек уже в локальном кэше — только ffplay по файлу с -ss
        (поиск + URL/HLS даёт сбои). Иначе полный поиск и стрим.
        """
        if self._cache is not None and track_id is not None:
            hit = self._cache.get_cached_path(str(track_id))
            if hit is not None and hit.is_file():
                logger.info(
                    "resume from disk cache track_id=%s seek=%.2fs path=%s",
                    track_id,
                    start_seconds,
                    hit,
                )
                return _popen_ffplay(str(hit), start_seconds), track_id
        return self.search_and_start_playback(query, start_seconds=start_seconds)

    def search_and_play(self, query: str) -> None:
        """
        Ищет треки по запросу и проигрывает первый найденный (блокирующий вызов).
        """
        proc, _ = self.search_and_start_playback(query)
        proc.wait()


if __name__ == "__main__":
    import sys

    query = sys.argv[1] if len(sys.argv) > 1 else "metallica"
    client = SoundcloudClient()
    client.search_and_play(query)

