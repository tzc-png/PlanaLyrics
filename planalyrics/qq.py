"""QQ Music's word-level lyrics: the online source tried after NetEase and Kugou.

The other two miss songs this one has, and where they do have one it is often not
the listener's release. Kugou answered 願い～あの頃のキミへ～ three times over -- the
English version, a remix, a sped-up edit -- and the timing that fitted came out
about 1.8 s ahead of the file; QQ had the release whose length matched to the
second, and whose word times sit 200-400 ms from the lyric's own.

What it hands over is not plain, which is why the module next door exists: a
deflated document with a Triple-DES of its own laid over it.
"""

from __future__ import annotations

import json
import re
import zlib
from dataclasses import dataclass

import requests

from . import qqdes
from .mpris import Track

SEARCH_URL = "https://c.y.qq.com/soso/fcgi-bin/client_search_cp"
LYRIC_URL = "https://u.y.qq.com/cgi-bin/musicu.fcg"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://y.qq.com/",
    "Content-Type": "application/json",
}
TIMEOUT = 8
# The caller stops at the first release whose timing lands on the words, so a
# longer list costs nothing until every one has been tried and failed.
MAX_CANDIDATES = 4

# the words arrive as an attribute of a one-line XML document, escaped, and with
# real newlines inside them -- which is why the document is not parsed as XML
_CONTENT_RE = re.compile(r'LyricContent="(.*?)"\s*/>', re.S)
_ESCAPES = (("&quot;", '"'), ("&apos;", "'"), ("&lt;", "<"), ("&gt;", ">"), ("&amp;", "&"))


@dataclass
class _Candidate:
    mid: str
    song_id: int
    title: str
    artist: str
    duration_ms: int = 0


def candidates(track: Track):
    """Every timing QQ Music offers for the song, the likeliest release first.

    Yielded one at a time so that the caller can stop at the first that lands on
    the words: each one costs a request of its own, and there is nothing to be
    gained from asking for the second once the first has fitted.

    Running out with nothing yielded means the site answered and had nothing. A
    site that never answered at all is raised instead, because the caller
    remembers what it is given and would go on believing this song has no timing
    anywhere.
    """
    if not track.title:
        return

    tried: set[str] = set()
    failures: list[Exception] = []
    answered = False
    for query in _queries(track):
        try:
            found = _search(query, track.length_ms)
        except (requests.RequestException, ValueError) as exc:
            failures.append(exc)
            continue
        answered = True
        for candidate in found[:MAX_CANDIDATES]:
            if candidate.mid in tried:
                continue
            tried.add(candidate.mid)
            try:
                text = _fetch(candidate)
            except (requests.RequestException, ValueError, zlib.error):
                continue
            if text.strip():
                yield text, f"QQ音乐: {candidate.title} - {candidate.artist}"
    if failures and not answered:
        raise failures[-1]


def _queries(track: Track) -> list[str]:
    title, artist = track.title.strip(), track.artist.strip()
    found: list[str] = []
    for query in (f"{title} {artist}".strip(), title):
        if query and query not in found:
            found.append(query)
    return found


def _search(query: str, length_ms: int = 0) -> list[_Candidate]:
    response = requests.get(
        SEARCH_URL,
        params={"w": query, "format": "json", "n": 10, "p": 1, "cr": 1, "g_tk": 5381},
        headers=HEADERS,
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    songs = ((response.json().get("data") or {}).get("song") or {}).get("list") or []
    found: list[_Candidate] = []
    for item in songs:
        mid = str(item.get("songmid") or "")
        if not mid:
            continue
        found.append(
            _Candidate(
                mid=mid,
                song_id=_to_int(item.get("songid")),
                title=str(item.get("songname") or ""),
                artist="/".join(
                    str(singer.get("name") or "") for singer in (item.get("singer") or [])
                ),
                duration_ms=_to_ms(item.get("interval")),
            )
        )
    if length_ms:
        # The same song turns up as several releases -- this version, that remix
        # -- and theirs is not the order that puts the listener's first. What
        # tells them apart is closeness in length; the ones with no length given
        # go last rather than sorting as zero.
        found.sort(key=lambda c: (c.duration_ms <= 0, abs(c.duration_ms - length_ms)))
    # With no length to measure against -- which is what the player reports when
    # it blanks the field -- the order the site gave is left alone. It is a
    # relevance ranking, and reordering by length instead does not pick a
    # different release of the song asked for; it picks a different song by the
    # same artist, whichever of those happens to run longest.
    return found


def _fetch(candidate: _Candidate) -> str:
    """One release's word timing, or "" where the site has none for it."""
    payload = {
        "comm": {"ct": 11, "cv": "1003006", "v": "1003006", "uin": "0", "format": "json"},
        "req": {
            "module": "music.musichallSong.PlayLyricInfo",
            "method": "GetPlayLyricInfo",
            "param": {
                "songMID": candidate.mid,
                "songID": candidate.song_id,
                "format": "json",
                "qrc": 1,
                "trans": 0,
                "roma": 0,
            },
        },
    }
    response = requests.post(
        LYRIC_URL, data=json.dumps(payload), headers=HEADERS, timeout=TIMEOUT
    )
    response.raise_for_status()
    data = (response.json().get("req") or {}).get("data") or {}
    if not data.get("qrc"):
        return ""                      # this release has no word timing here
    encoded = data.get("lyric") or ""
    if not encoded:
        return ""
    return _lyric_content(qqdes.decrypt(encoded))


def _lyric_content(document: str) -> str:
    match = _CONTENT_RE.search(document)
    if match is None:
        return ""
    text = match.group(1)
    for escaped, plain in _ESCAPES:
        text = text.replace(escaped, plain)
    return text


def _to_ms(value) -> int:
    """QQ reports a length in whole seconds; this program counts milliseconds."""
    try:
        return int(float(value or 0)) * 1000
    except (TypeError, ValueError):
        return 0


def _to_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
