"""Online lyrics lookup, backed by NetEase Cloud Music.

NetEase serves the original and its translation as two separate timestamped
documents, which is exactly the shape the overlay wants.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

import requests

from .mpris import Track

SEARCH_URL = "https://music.163.com/api/search/get"
LYRIC_URL = "https://music.163.com/api/song/lyric"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://music.163.com/",
    "Accept": "application/json, text/plain, */*",
}
TIMEOUT = 8
MAX_CANDIDATES = 3
DURATION_TOLERANCE_MS = 4000
_STAMP_RE = re.compile(r"\[\d{1,3}:\d{1,2}")
_SPACE_RE = re.compile(r"\s+")
_BRACKETS_RE = re.compile(r"[【\[（(][^】\]）)]*[】\]）)]")
# video-title noise: a trailing tag like "【Official MV】" or "- 完整版"
_NOISE_TAIL_RE = re.compile(
    r"\s*[-–—|]\s*(official|offical|mv|pv|op|ed|full|hd|4k|完整版|官方|高清|"
    r"中日双语|中文字幕|中英字幕|双语字幕|字幕|翻唱|翻自|cover|ver\.?|"
    r"music\s*video|lyric\s*video)\b.*$",
    re.IGNORECASE,
)
_SPLIT_RE = re.compile(r"\s+[-–—/|]\s+")
# the same tags stuck on the end with no separator, repeated as needed -- which is
# how Bilibili's page titles arrive: "Bad apple！！！坏苹果！！！_哔哩哔哩_bilibili"
_NOISE_END_RE = re.compile(
    r"([\s_\-–—|]*(?:"
    r"中日双语|中文字幕|中英字幕|双语字幕|中文翻译|字幕|完整版|官方|高清|无损|"
    r"official|mv|pv|op|ed|cover|翻唱|4k|hd|bilibili|哔哩哔哩|哔哩"
    r"))+$",
    re.IGNORECASE,
)


def _clean_title(title: str) -> str:
    """Strip the packaging off a video title so a song can be searched for."""
    text = _BRACKETS_RE.sub(" ", title or "")
    text = _NOISE_TAIL_RE.sub("", text)
    text = _NOISE_END_RE.sub("", text)
    return _SPACE_RE.sub(" ", text).strip(" -–—/|")


def queries(track: Track) -> list[str]:
    """Candidate search strings, best first.

    Browsers report the whole video title (uploader, 「」 brackets, "【中日双语】"
    and so on), so the cleaned title is tried with and without the artist and,
    for bilingual titles, split on their separators.
    """
    found: list[str] = []

    def add(candidate: str) -> None:
        candidate = _SPACE_RE.sub(" ", (candidate or "").strip())
        if candidate and candidate.lower() not in {item.lower() for item in found}:
            found.append(candidate)

    cleaned = _clean_title(track.title)
    if track.artist:
        add(f"{cleaned} {track.artist}")
    add(cleaned)
    for part in _SPLIT_RE.split(cleaned):
        part = part.strip()
        if part and part != cleaned:
            if track.artist:
                add(f"{part} {track.artist}")
            add(part)
    if cleaned != track.title:
        add(track.title)
    return found[:6]


@dataclass
class Candidate:
    song_id: int
    title: str
    artist: str
    duration_ms: int
    score: float


def lookup(track: Track) -> tuple[str, str, str, str] | None:
    """Return ``(original_lrc, translation_lrc, yrc, label)``, or None."""
    if not track.title:
        return None

    # Score candidates against the *cleaned* title: left as the raw video title,
    # words like the series name ("废墟图书馆") make a cover version look like a
    # better match than the original.
    reference = Track(
        title=_clean_title(track.title),
        artist=track.artist,
        length_ms=track.length_ms,
    )

    tried: set[int] = set()
    fallback: tuple[str, str, str, str] | None = None
    for query in queries(track):
        try:
            candidates = search(query, reference)
        except (requests.RequestException, ValueError):
            continue
        for candidate in candidates[:MAX_CANDIDATES]:
            if candidate.song_id in tried:
                continue
            tried.add(candidate.song_id)
            try:
                original, translation, yrc = _lyrics(candidate.song_id)
            except (requests.RequestException, ValueError):
                continue
            if not original.strip():
                continue
            label = f"网易云: {candidate.title} - {candidate.artist}"
            if _STAMP_RE.search(original):
                return original, translation, yrc, label
            fallback = fallback or (original, translation, yrc, label)
    return fallback


def search(query: str, track: Track) -> list[Candidate]:
    response = requests.get(
        SEARCH_URL,
        params={"s": query, "type": 1, "limit": 10, "offset": 0},
        headers=HEADERS,
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    songs = ((response.json().get("result") or {}).get("songs")) or []

    candidates = []
    for song in songs:
        candidate = Candidate(
            song_id=int(song.get("id") or 0),
            title=str(song.get("name") or ""),
            artist=" / ".join(str(a.get("name")) for a in song.get("artists") or [] if a),
            duration_ms=int(song.get("duration") or 0),
            score=0.0,
        )
        candidate.score = _score(candidate, track)
        candidates.append(candidate)
    candidates.sort(key=lambda item: item.score, reverse=True)
    return candidates


def _score(candidate: Candidate, track: Track) -> float:
    score = 3 * _ratio(candidate.title, track.title)
    if track.artist:
        score += 2 * _ratio(candidate.artist, track.artist)
    if track.length_ms and candidate.duration_ms:
        delta = abs(candidate.duration_ms - track.length_ms)
        if delta <= DURATION_TOLERANCE_MS:
            score += 3
        elif delta <= 2 * DURATION_TOLERANCE_MS:
            score += 1
    return score


def _lyrics(song_id: int) -> tuple[str, str, str]:
    """``(original, translation, yrc)`` for one song.

    ``os=pc`` asks for the desktop flavour, the one that also serves the
    word-level ``yrc``; the same request returns the same ``lrc`` and ``tlyric``
    as before, so nothing changes for the songs that have no ``yrc`` to offer.
    """
    response = requests.get(
        LYRIC_URL,
        params={"os": "pc", "id": song_id, "lv": 1, "tv": 1, "kv": 1, "rv": 1, "yv": 1},
        headers=HEADERS,
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()
    original = (payload.get("lrc") or {}).get("lyric") or ""
    translation = (payload.get("tlyric") or {}).get("lyric") or ""
    yrc = (payload.get("yrc") or {}).get("lyric") or ""
    return original, translation, yrc


def _ratio(left: str, right: str) -> float:
    left, right = _normalize(left), _normalize(right)
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right).ratio()


def _normalize(text: str) -> str:
    return _SPACE_RE.sub("", (text or "").casefold())
