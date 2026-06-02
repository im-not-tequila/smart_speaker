"""
Локальный кэш загруженных треков SoundCloud по id, лимит по суммарному размеру (LRU).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

MANIFEST_NAME = "manifest.json"


def _file_starts_as_hls_playlist(path: Path) -> bool:
    try:
        head = path.read_bytes()[:64]
    except OSError:
        return False
    return _is_hls_playlist_bytes(head)


def _is_hls_playlist_bytes(data: bytes) -> bool:
    if not data:
        return False
    s = data.lstrip(b"\xef\xbb\xbf")
    return s.startswith(b"#EXTM3U") or s.startswith(b"#EXT-X-")


class TrackCache:
    def __init__(self, root: Path, max_bytes: int):
        self.root = Path(root)
        self.max_bytes = max_bytes
        self._lock = threading.Lock()
        self.root.mkdir(parents=True, exist_ok=True)
        self._manifest_path = self.root / MANIFEST_NAME

    def _load_lru(self) -> list[dict]:
        if not self._manifest_path.is_file():
            return []
        try:
            data = json.loads(self._manifest_path.read_text(encoding="utf-8"))
            lru = data.get("lru", [])
            if isinstance(lru, list):
                return lru
        except (json.JSONDecodeError, OSError):
            pass
        return []

    def _save_lru(self, lru: list[dict]) -> None:
        tmp = self._manifest_path.with_suffix(".json.tmp")
        payload = {"version": 1, "lru": lru}
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=0), encoding="utf-8")
        tmp.replace(self._manifest_path)

    def _prune_missing_files(self, lru: list[dict]) -> list[dict]:
        cleaned: list[dict] = []
        dropped = 0
        for e in lru:
            file_name = e.get("file", f"{e.get('id')}.mp3")
            if (self.root / file_name).is_file():
                cleaned.append(e)
            else:
                dropped += 1
        if dropped:
            logger.info("soundcloud cache prune missing files: dropped=%s", dropped)
        return cleaned

    def file_path_for(self, track_id: str) -> Path:
        return self.root / f"{track_id}.mp3"

    def get_cached_path(self, track_id: str) -> Path | None:
        """Если файл есть и запись в манифесте согласована — путь; иначе None."""
        sid = str(track_id)
        path = self.file_path_for(sid)
        if not path.is_file():
            with self._lock:
                original = self._load_lru()
                lru = self._prune_missing_files(original)
                new_lru = [e for e in lru if e.get("id") != sid]
                if len(new_lru) != len(original):
                    self._save_lru(new_lru)
            return None
        with self._lock:
            original = self._load_lru()
            lru = self._prune_missing_files(original)
            idx = next((i for i, e in enumerate(lru) if e.get("id") == sid), None)
            if idx is None:
                if len(lru) != len(original):
                    self._save_lru(lru)
                return None
            if _file_starts_as_hls_playlist(path):
                try:
                    path.unlink()
                except OSError:
                    pass
                lru = [e for e in lru if e.get("id") != sid]
                self._save_lru(lru)
                logger.info(
                    "soundcloud cache drop bogus HLS-as-mp3 track_id=%s",
                    sid,
                )
                return None
            entry = lru.pop(idx)
            lru.append(entry)
            self._save_lru(lru)
        logger.info(
            "soundcloud cache hit track_id=%s path=%s size=%s",
            sid,
            path,
            path.stat().st_size,
        )
        return path

    def _evict_until_budget(self, lru: list[dict], budget: int) -> None:
        total = sum(int(e.get("size", 0)) for e in lru)
        while total > budget and len(lru) > 1:
            victim = lru.pop(0)
            vid = victim.get("id")
            fpath = self.root / victim.get("file", f"{vid}.mp3")
            sz = int(victim.get("size", 0))
            try:
                if fpath.is_file():
                    fpath.unlink()
                logger.info(
                    "soundcloud cache evict track_id=%s removed_bytes=%s (budget=%s)",
                    vid,
                    sz,
                    budget,
                )
            except OSError as e:
                logger.warning("soundcloud cache evict failed path=%s: %s", fpath, e)
            total -= sz

    def register_download(self, track_id: str, file_path: Path) -> None:
        """После успешной записи файла: учесть размер, LRU, вытеснение по лимиту."""
        sid = str(track_id)
        path = Path(file_path)
        if not path.is_file():
            return
        size = path.stat().st_size
        rel = path.name
        with self._lock:
            lru = self._load_lru()
            lru = self._prune_missing_files(lru)
            lru = [e for e in lru if e.get("id") != sid]
            lru.append(
                {
                    "id": sid,
                    "file": rel,
                    "size": size,
                    "ts": time.time(),
                }
            )
            self._evict_until_budget(lru, self.max_bytes)
            self._save_lru(lru)
        logger.info(
            "soundcloud cache store track_id=%s path=%s size=%s total_tracks=%s",
            sid,
            path,
            size,
            len(lru),
        )
