"""Where lyrics come from, in priority order.

A sidecar .lrc next to the audio file, then lyrics embedded in the audio file's
own tags, then a previously cached online result, then a live online search.
Whatever the network returns is cached, so the next play of that song is local.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import re
from pathlib import Path

import mutagen

from . import lyrics as lrc
from . import net
from .lyrics import Lyrics
from .mpris import Track

LANG_SUFFIXES = (
    "", ".zh", ".zh_CN", ".zh_cn", ".zh-Hans", ".zh-Hant",
    ".chs", ".cht", ".sc", ".tc", ".ja", ".jp", ".en",
)
_LYRIC_KEYS = ("lyrics", "unsyncedlyrics", "syncedlyrics", "lyric", "©lyr")
# a one- or two-line tag is usually a placeholder stub, so a fuller source may
# win over it -- but a deliberate notice like "纯音乐，请欣赏" is the real lyric
# of an instrumental and is kept as-is
MIN_USEFUL_LINES = 3
INSTRUMENTAL_RE = re.compile(
    r"纯音乐|请欣赏|仅音乐|纯乐器|无歌词|暂无歌词|没有歌词|instrumental", re.IGNORECASE
)


def resolve(
    audio_path: Path | None,
    track: Track,
    cache_dir: Path,
    allow_online: bool = True,
    use_cache: bool = True,
    preferred_text: str | None = None,
) -> Lyrics | None:
    """First source with a usable lyric wins.

    The audio file's own lyrics come first (the user's own data), then the lyric
    the player itself is displaying -- which matches that player's clock exactly
    -- then the cache, then the network. ``use_cache`` is cleared for a manual
    re-search, which should query the network rather than replay a cached answer.
    """
    candidates = [
        _from_sidecar(audio_path),
        _from_embedded(audio_path),
        _from_player(preferred_text, track, allow_online),
    ]
    if use_cache:
        candidates.append(_from_cache(track, cache_dir))

    stub: Lyrics | None = None
    for lyrics in candidates:
        if lyrics is None or not lyrics.lines:
            continue
        if len(lyrics.lines) >= MIN_USEFUL_LINES or _is_instrumental_notice(lyrics):
            return lyrics
        stub = stub or lyrics

    online = _from_online(track, cache_dir) if allow_online else None
    return online or stub


def _is_instrumental_notice(lyrics: Lyrics) -> bool:
    return any(INSTRUMENTAL_RE.search(line.text) for line in lyrics.lines)


def _from_player(text: str | None, track: Track, allow_online: bool) -> Lyrics | None:
    if not text or not text.strip():
        return None
    parsed = lrc.parse(text)
    if not parsed.lines:
        return None
    parsed.source = "播放器自带歌词"
    if allow_online and parsed.wants_translation and not any(line.translation for line in parsed.lines):
        _borrow_translations(parsed, track)
    return parsed


def _borrow_translations(parsed: Lyrics, track: Track) -> None:
    """The player supplied originals only; take the translation from a match.

    Timestamps rarely agree to the millisecond across sources, so translations
    are matched to the nearest line rather than by exact time.
    """
    result = net.lookup(track)
    if result is None:
        return
    original, translation, _label = result
    if not translation.strip():
        return
    other = lrc.parse_with_translation(original, translation)
    _map_translations(parsed, other.lines)


def _map_translations(target: Lyrics, source: list, tolerance_ms: int = 400) -> None:
    times = [line.t_ms for line in source]
    for line in target.lines:
        position = bisect.bisect_left(times, line.t_ms)
        best: tuple[int, str] | None = None
        for index in (position - 1, position):
            if not 0 <= index < len(source):
                continue
            delta = abs(source[index].t_ms - line.t_ms)
            if delta <= tolerance_ms and source[index].translation:
                if best is None or delta < best[0]:
                    best = (delta, source[index].translation)
        if best is not None:
            line.translation = best[1]


# -- local sources -------------------------------------------------------


def _from_sidecar(audio_path: Path | None) -> Lyrics | None:
    if audio_path is None:
        return None
    for candidate in _sidecar_candidates(audio_path):
        try:
            parsed = lrc.parse_file(candidate)
        except OSError:
            continue
        if parsed.lines:
            parsed.source = candidate.name
            return parsed
    return None


def _sidecar_candidates(audio_path: Path) -> list[Path]:
    candidates = [audio_path.with_suffix(f"{suffix}.lrc") for suffix in LANG_SUFFIXES]
    candidates.append(audio_path.with_suffix(".LRC"))
    seen: set[Path] = set()
    unique = []
    for candidate in candidates:
        if candidate not in seen:
            seen.add(candidate)
            unique.append(candidate)
    return unique


def _from_embedded(audio_path: Path | None) -> Lyrics | None:
    if audio_path is None:
        return None
    text = read_embedded_text(audio_path)
    if not text:
        return None
    parsed = lrc.parse(text)
    if parsed.lines:
        parsed.source = "内嵌标签"
    return parsed


def read_embedded_text(audio_path: Path) -> str | None:
    try:
        audio = mutagen.File(audio_path)
        keys = [str(key) for key in audio.keys()] if audio is not None else []
    except Exception:
        return None
    if audio is None:
        return None

    candidates = list(_LYRIC_KEYS)
    candidates += [key for key in keys if key.lower().startswith("uslt")]
    for key in candidates:
        text = _tag_text(_tag_value(audio, key))
        if text and text.strip():
            return text
    return None


def _tag_value(audio, key: str):
    """Read a tag, tolerating the encodings mutagen's containers use.

    A Vorbis comment container raises ValueError rather than returning None for
    an absent key, and ID3 raises KeyError, so both must be swallowed.
    """
    try:
        return audio[key]
    except Exception:
        return None


def _tag_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return "\n".join(str(item) for item in value if item)
    text = getattr(value, "text", None)
    if text is None:
        return str(value)
    if isinstance(text, (list, tuple)):
        return "\n".join(str(item) for item in text if item)
    return str(text)


# -- cached and online sources -------------------------------------------


def _cache_path(track: Track, cache_dir: Path) -> Path | None:
    if not track.title:
        return None
    key = hashlib.sha1(
        f"{track.title}\x00{track.artist}\x00{track.length_ms}".encode("utf-8")
    ).hexdigest()
    return cache_dir / "lyrics" / f"{key}.json"


def _from_cache(track: Track, cache_dir: Path) -> Lyrics | None:
    path = _cache_path(track, cache_dir)
    if path is None:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    original = payload.get("lrc") or ""
    if not original.strip():
        return None
    parsed = lrc.parse_with_translation(original, payload.get("tlrc") or "")
    if parsed.lines:
        parsed.source = f"缓存: {payload.get('label') or '网易云'}"
    return parsed


def _from_online(track: Track, cache_dir: Path) -> Lyrics | None:
    result = net.lookup(track)
    if result is None:
        return None
    original, translation, label = result
    parsed = lrc.parse_with_translation(original, translation)
    if not parsed.lines:
        return None
    parsed.source = label
    _save_cache(track, cache_dir, original, translation, label)
    return parsed


def _save_cache(track: Track, cache_dir: Path, original: str, translation: str, label: str) -> None:
    path = _cache_path(track, cache_dir)
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"lrc": original, "tlrc": translation, "label": label},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"PlanaLyrics: 无法写入歌词缓存 ({exc})")
