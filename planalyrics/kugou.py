"""Kugou's word-level lyrics: the online source tried after NetEase.

NetEase serves word timing for some songs and nothing for many others -- two of
five on the songs tried here, and none at all for some well-known ones. Kugou had
it for all five, which is why it is here.

Kugou hands the timing over as a small binary blob (krc): base64, a keyed xor,
then deflate. That is plainer than QQ Music's equivalent, which is encrypted, and
it decodes with nothing but the standard library.
"""

from __future__ import annotations

import base64
import re
import zlib
from dataclasses import dataclass

import requests

from .mpris import Track

SEARCH_URL = "https://krcs.kugou.com/search"
DOWNLOAD_URL = "https://lyrics.kugou.com/download"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}
TIMEOUT = 8
MAX_CANDIDATES = 3
# the blob is xored with this key and then deflated; the four bytes it opens with
# are a marker, not part of the stream
_KEY = bytes(
    [0x40, 0x47, 0x61, 0x77, 0x5E, 0x32, 0x74, 0x47,
     0x51, 0x36, 0x31, 0x2D, 0xCE, 0xD2, 0x6E, 0x69]
)


# Kugou's search will not match a title that still carries punctuation: 「恋せよ
# 乙女！」 comes back empty where 「恋せよ乙女」 finds ten. The title is therefore
# offered with its punctuation turned into spaces first, and untouched after that,
# so nothing is lost where the punctuation was the point.
_PUNCT_RE = re.compile(r"[^\w]+")


def _plain(text: str) -> str:
    return _PUNCT_RE.sub(" ", text).strip()


def _queries(track: Track) -> list[str]:
    title, artist = track.title.strip(), track.artist.strip()
    found: list[str] = []
    for name in (_plain(title), title):
        for candidate in (f"{name} {artist}".strip(), name):
            if name and candidate and candidate not in found:
                found.append(candidate)
    return found


@dataclass
class _Candidate:
    id: str
    accesskey: str
    title: str
    artist: str
    duration_ms: int = 0


def lookup(track: Track) -> tuple[str, str] | None:
    """Return ``(krc_text, label)`` for the song, or None."""
    if not track.title:
        return None

    tried: set[str] = set()
    for query in _queries(track):
        try:
            candidates = _search(query, track.length_ms)
        except (requests.RequestException, ValueError):
            continue
        for candidate in candidates[:MAX_CANDIDATES]:
            if candidate.id in tried:
                continue
            tried.add(candidate.id)
            try:
                text = _fetch(candidate)
            except (requests.RequestException, ValueError, zlib.error):
                continue
            if text.strip():
                return text, f"酷狗: {candidate.title} - {candidate.artist}"
    return None


def _search(query: str, length_ms: int = 0) -> list[_Candidate]:
    response = requests.get(
        SEARCH_URL,
        params={"ver": 1, "man": "yes", "client": "mobi", "keyword": query},
        headers=HEADERS,
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    found = []
    for item in (response.json().get("candidates") or []):
        candidate = _Candidate(
            id=str(item.get("id") or ""),
            accesskey=str(item.get("accesskey") or ""),
            title=str(item.get("song") or ""),
            artist=str(item.get("singer") or ""),
            duration_ms=_to_ms(item.get("duration")),
        )
        if candidate.id and candidate.accesskey:
            found.append(candidate)
    if length_ms:
        # The same song turns up as several cuts -- a full version and a short
        # edit, say -- and theirs is not the order that puts the right one first.
        # Closeness in length is what tells them apart; the ones with no length
        # given go last rather than sorting as zero.
        found.sort(key=lambda c: (c.duration_ms <= 0, abs(c.duration_ms - length_ms)))
    return found


def _to_ms(value) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _fetch(candidate: _Candidate) -> str:
    response = requests.get(
        DOWNLOAD_URL,
        params={"ver": 1, "client": "pc", "id": candidate.id,
                "accesskey": candidate.accesskey, "fmt": "krc", "charset": "utf8"},
        headers=HEADERS,
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    content = response.json().get("content") or ""
    return _decode(base64.b64decode(content)) if content else ""


def _decode(blob: bytes) -> str:
    """Undo Kugou's blob: a keyed xor, then deflate, after the four-byte marker."""
    plain = bytes(byte ^ _KEY[index % len(_KEY)] for index, byte in enumerate(blob[4:]))
    return zlib.decompress(plain).decode("utf-8", "replace")
