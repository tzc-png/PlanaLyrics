"""Find the audio file behind the currently playing track.

Not every player publishes ``xesam:url``, so the path has to be recovered: first
from the player's open file descriptors (authoritative and instant), then from a
directory index matched on tags and duration.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

import mutagen

from .mpris import Track

AUDIO_EXTS = {
    ".aac", ".aif", ".aiff", ".alac", ".ape", ".flac", ".m4a", ".mp3", ".mpc",
    ".oga", ".ogg", ".opus", ".wav", ".wma", ".wv",
}
DURATION_TOLERANCE_MS = 2000
INDEX_VERSION = 1
_SPACE_RE = re.compile(r"\s+")


def locate(track: Track, music_dirs: list[Path], cache_dir: Path, pid: int | None) -> Path | None:
    url_path = path_from_url(track.url)
    if url_path is not None:
        return url_path
    if is_remote(track.url):
        # streaming: the player's open descriptors would only be its cache blob
        return None

    candidates = open_audio_files(pid) if pid else []
    match = _best_match(candidates, track)
    if match:
        return match
    if len(candidates) == 1:
        return candidates[0]

    index = _load_index(cache_dir, music_dirs)
    if index is not None:
        return _index_lookup(index, track)

    index = _build_index(music_dirs)
    _save_index(cache_dir, index)
    return _index_lookup(index, track)


def path_from_url(url: str) -> Path | None:
    """A local file the player told us about over MPRIS."""
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme != "file":
        return None
    path = Path(unquote(parsed.path))
    return path if path.is_file() else None


def is_remote(url: str) -> bool:
    return urlparse(url).scheme in ("http", "https", "rtmp", "rtsp")


def open_audio_files(pid: int) -> list[Path]:
    found: list[Path] = []
    try:
        descriptors = list(Path(f"/proc/{pid}/fd").iterdir())
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        return found

    for descriptor in descriptors:
        try:
            target = os.readlink(descriptor)
        except OSError:
            continue
        if target.endswith(" (deleted)"):
            continue
        path = Path(target)
        if path.suffix.lower() in AUDIO_EXTS and path.is_file() and path not in found:
            found.append(path)
    return found


def read_tags(path: Path) -> tuple[str, str, int] | None:
    try:
        audio = mutagen.File(path, easy=True)
    except Exception:
        return None
    if audio is None:
        return None
    info = getattr(audio, "info", None)
    duration_ms = int(getattr(info, "length", 0) * 1000) if info else 0
    return _first(audio, "title"), _first(audio, "artist") or _first(audio, "albumartist"), duration_ms


def _first(audio, key: str) -> str:
    try:
        value = audio.get(key)
    except Exception:
        return ""
    if isinstance(value, (list, tuple)):
        return str(value[0]) if value else ""
    return str(value) if value else ""


def _best_match(candidates: list[Path], track: Track) -> Path | None:
    if not candidates:
        return None
    scored: list[tuple[int, Path]] = []
    for path in candidates:
        tags = read_tags(path)
        if not tags:
            continue
        title, artist, duration_ms = tags
        score = 0
        if track.title and _normalize(title) == _normalize(track.title):
            score += 2
        if track.artist and _normalize(artist) == _normalize(track.artist):
            score += 1
        if track.length_ms and duration_ms and abs(duration_ms - track.length_ms) <= DURATION_TOLERANCE_MS:
            score += 1
        if score:
            scored.append((score, path))
    if not scored:
        return None
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored[0][1]


def _index_lookup(index: dict, track: Track) -> Path | None:
    entries = index.get("entries") or {}
    if not track.title:
        return None

    keys = []
    title = _normalize(track.title)
    if track.artist:
        keys.append(f"{title}|{_normalize(track.artist)}")
    keys.extend(key for key in entries if key.startswith(f"{title}|"))

    for key in keys:
        for path, duration_ms in entries.get(key, []):
            candidate = Path(path)
            if not candidate.is_file():
                continue
            if track.length_ms and duration_ms:
                if abs(int(duration_ms) - track.length_ms) > DURATION_TOLERANCE_MS:
                    continue
            return candidate
    return None


def _build_index(music_dirs: list[Path]) -> dict:
    entries: dict[str, list[list]] = {}
    states: dict[str, float] = {}
    for directory in music_dirs:
        try:
            states[str(directory)] = directory.stat().st_mtime
        except OSError:
            continue
        for path in directory.rglob("*"):
            if path.suffix.lower() not in AUDIO_EXTS or not path.is_file():
                continue
            tags = read_tags(path)
            if not tags or not tags[0]:
                continue
            title, artist, duration_ms = tags
            entries.setdefault(f"{_normalize(title)}|{_normalize(artist)}", []).append(
                [str(path), duration_ms]
            )
    return {"version": INDEX_VERSION, "dirs": states, "entries": entries}


def _index_path(cache_dir: Path) -> Path:
    return cache_dir / "index.json"


def _dir_states(music_dirs: list[Path]) -> dict[str, float]:
    states: dict[str, float] = {}
    for directory in music_dirs:
        try:
            states[str(directory)] = directory.stat().st_mtime
        except OSError:
            continue
    return states


def _load_index(cache_dir: Path, music_dirs: list[Path]) -> dict | None:
    try:
        index = json.loads(_index_path(cache_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if index.get("version") != INDEX_VERSION:
        return None
    if index.get("dirs") != _dir_states(music_dirs):
        return None
    if abs(index.get("built", 0) - time.time()) > 7 * 24 * 3600:
        return None
    return index


def _save_index(cache_dir: Path, index: dict) -> None:
    index["built"] = time.time()
    try:
        path = _index_path(cache_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _normalize(text: str) -> str:
    return _SPACE_RE.sub(" ", (text or "").strip()).casefold()
