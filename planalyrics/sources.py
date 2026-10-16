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
import time
from pathlib import Path

import mutagen

from . import kugou
from . import lyrics as lrc
from . import net
from . import qq
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
# 纯音乐 and nothing else. A notice can put it several ways -- 请欣赏 on its own,
# 仅音乐, 纯乐器, 无歌词, 暂无歌词, "instrumental" -- and the rest of them are left
# to be searched for on purpose: 无歌词 and 暂无歌词 say the words are missing,
# which is a reason to go looking for them, and it is 纯音乐 that says there are
# none to find.
INSTRUMENTAL_RE = re.compile(r"纯音乐")


def resolve(
    audio_path: Path | None,
    track: Track,
    cache_dir: Path,
    allow_online: bool = True,
    use_cache: bool = True,
    preferred_text: str | None = None,
    search_translation: bool = True,
    online_timing: bool = True,
) -> Lyrics | None:
    """First source with a usable lyric wins.

    The audio file's own lyrics come first (the user's own data), then the lyric
    the player itself is displaying -- which matches that player's clock exactly
    -- then a saved result, then the network. ``use_cache`` is cleared for a
    manual re-search, which should query the network rather than replay a cached
    answer.

    ``allow_online`` governs searching for the lyrics themselves: with it off
    nothing that came from a search is used, the saved results included, so the
    settings file's switch changes what is on screen rather than appearing not to
    have taken.

    ``online_timing`` is the word timing's own switch, separate from that one:
    wanting the timing from the network is a different wish from wanting the words
    from it, and the timing is fetched even for a lyric that came off the disk.

    ``search_translation`` decides where a Chinese line may come from. Left on, a
    foreign-language song (Japanese, Korean, ...) whose own lyrics carry no
    Chinese borrows one from a match; turned off, the only Chinese shown is the
    Chinese that came with the lyrics.
    """
    chosen: Lyrics | None = None
    stub: Lyrics | None = None
    for lyrics in (
        _from_sidecar(audio_path),
        _from_embedded(audio_path),
        _from_player(preferred_text),
    ):
        if lyrics is None or not lyrics.lines:
            continue
        if len(lyrics.lines) >= MIN_USEFUL_LINES or _is_instrumental_notice(lyrics):
            chosen = lyrics
            break
        stub = stub or lyrics

    if chosen is not None:
        if _is_instrumental_notice(chosen):
            return chosen
        if search_translation and allow_online:
            _add_translation(chosen, track, cache_dir)
        _add_timing(chosen, track, cache_dir, online_timing)
        chosen.fit_timelines()
        return chosen

    # Nothing of the listener's own. A saved result counts as a searched one --
    # it is what a search returned last time -- so it is only consulted when
    # searching is allowed at all; with the search off, a song whose lyric came
    # from the network shows nothing rather than the same network lyric it showed
    # before. Turning the search off therefore changes what is on screen at once.
    fetched = None
    if allow_online:
        fetched = _from_cache(track, cache_dir) if use_cache else None
        if fetched is None:
            fetched = _from_online(track, cache_dir)
    if fetched is None:
        return stub
    if not search_translation:
        _drop_translations(fetched)
    if _is_instrumental_notice(fetched):
        return fetched
    _add_timing(fetched, track, cache_dir, online_timing)
    fetched.fit_timelines()
    return fetched


def _is_instrumental_notice(lyrics: Lyrics) -> bool:
    """Whether the lyric says there are no words rather than being them.

    "纯音乐，请欣赏" is the whole of what a file's tags hold for an instrumental, so
    it is taken as the song's lyric rather than passed over for a stub. It is also
    the end of the matter: there are no words to translate and none to give a word
    timing to, so neither is searched for. Every one of those searches would be a
    request spent on a song that has nothing to search for, and the timing in
    particular can only come back wrong -- the sites do carry lines for
    instrumentals, and putting one of those on a two-line notice would light the
    notice up word by word as if it were being sung.
    """
    return any(INSTRUMENTAL_RE.search(line.text) for line in lyrics.lines)


def _from_player(text: str | None) -> Lyrics | None:
    if not text or not text.strip():
        return None
    parsed = lrc.parse(text)
    if not parsed.lines:
        return None
    parsed.source = "播放器自带歌词"
    return parsed


def _add_translation(parsed: Lyrics, track: Track, cache_dir: Path) -> None:
    """Give a foreign-language original the Chinese it does not carry itself.

    The saved result is consulted before the network so a song is only looked up
    once. Timestamps rarely agree to the millisecond across sources, so
    translations are matched to the nearest line rather than by exact time.

    Called only where searching is allowed, since both the saved result and the
    network answer come from a search.
    """
    if not parsed.wants_translation or any(line.translation for line in parsed.lines):
        return
    other = _from_cache(track, cache_dir)
    if other is None:
        other = _from_online(track, cache_dir)
    if other is not None:
        _map_translations(parsed, other.lines)


def _drop_translations(parsed: Lyrics) -> None:
    for line in parsed.lines:
        line.translation = None


def _add_timing(parsed: Lyrics, track: Track, cache_dir: Path, allowed: bool) -> None:
    """Ask the online sites for the word timing the lyric did not come with.

    A lyric read from a file carries only the timing the file happened to hold,
    which is usually none, so the sites are asked in turn and the first with
    something that fits is the one kept. Each site is asked about a song once and
    its answer remembered -- an empty answer included, so a song with no timing
    anywhere is not looked up again on every play.

    ``allowed`` is the word timing's own switch, not the one for searching
    lyrics: wanting the timing from the network is a separate wish from wanting
    the lyrics from it.

    A lyric already carrying its own awlrc is left alone. That is the timing the
    sweep shows in preference to anything a site can add -- awlrc comes first of
    the three in TIMELINE_SOURCES, in overlay.py, which is where that order is
    set out -- so a search here is one whose answer is never displayed. It is also
    the slowest thing this program does: two sites searched, each with the
    downloads that follow, on every song opened for the first time.
    """
    if not allowed:
        return
    if parsed.timing_covers("awlrc"):
        return                         # its own timing is the one that gets shown
    for source, fetch in ONLINE_TIMING:
        if any(line.timelines.get(source) for line in parsed.lines):
            return                     # this site's timing is already in hand
        if _place_site_timing(parsed, track, cache_dir, source, fetch):
            return


def _place_site_timing(parsed: Lyrics, track: Track, cache_dir: Path, source: str, fetch) -> bool:
    """Put one site's timing onto the lyric, taking a cut that fits.

    What the site offered last time is tried first, so a song whose timing is
    already known costs no request at all. Failing that the site is asked, and
    every cut it offers is tried rather than only the one it put first: the same
    song sits on these sites several times over -- the full version, a short
    edit, a version sung in another language -- and the likeliest by length is
    not necessarily the one this lyric is a version of.

    Only a timing that lands on the words is taken, and only that is remembered.
    A candidate was once taken for carrying any text at all, which is how the
    English cut of a Japanese song came to be written to the cache on every play
    and left that song on the sweep for good.
    """
    path = _timing_path(track, cache_dir, source)
    remembered = _read_answer(path)
    if remembered:
        if _put_timing(parsed, source, remembered):
            return True
        # a remembered answer that no longer fits is not the end of it: the site
        # is asked again, so a better cut can replace it rather than be shut out
        # by it for as long as the cache lives
    elif remembered is not None:
        return False                   # believed to have nothing, for now
    try:
        offered = fetch(track)
    except Exception:
        return False                   # never reached the site; remember nothing
    try:
        for text, _label in offered:
            if not text or text == remembered:
                continue
            if _put_timing(parsed, source, text):
                _write_answer(path, text)
                return True
    except Exception:
        return False                   # never reached the site; remember nothing
    _write_answer(path, "")
    return False


def _put_timing(parsed: Lyrics, source: str, text: str) -> bool:
    """Lay one timing text over the lyric, saying whether it reaches the song.

    Line for line first, which is exact where the two are cut the same way;
    whatever that cannot place is offered to the alignment, which reaches it by
    lining the two texts up instead.

    Landing on a line or two is not enough. A timing from a short edit of the
    song fits the handful of lines the edit kept and nothing else, and taking
    that leaves the sweep stopping and starting down the song, which reads as the
    feature being broken rather than as the site having the wrong cut. What is
    taken has to reach half the lines; anything less is taken back off the lines
    it did reach, so the caller's next candidate starts from a clean lyric.
    """
    table = lrc.parse_timing(source, text)
    lrc.attach_timelines(parsed, source, table)
    lrc.align_timeline(parsed, table, source)
    if parsed.timing_covers(source):
        return True
    for line in parsed.lines:
        line.timelines.pop(source, None)
    return False


def _read_answer(path: Path | None) -> str | None:
    """What is remembered for a site, or None when it has to be asked again.

    An empty answer is remembered too, but only for an hour. The sites drop a
    request now and then and answer with nothing when they do, which reads
    exactly like the song not being there -- so an empty answer is believed for
    an hour and then asked about again, while one with timing in it is kept
    because timing does not go stale.
    """
    if path is None:
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if text:
        return text
    try:
        fresh = time.time() - path.stat().st_mtime < EMPTY_ANSWER_TTL_S
    except OSError:
        return None
    return "" if fresh else None


def _write_answer(path: Path | None, text: str) -> None:
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        print(f"PlanaLyrics: 无法写入逐字缓存 ({exc})")


def _timing_from_netease(track: Track):
    """NetEase has at most one timing to give: its ``yrc``, or none."""
    result = net.lookup(track)
    if result is None:
        return
    _original, _translation, yrc, label = result
    if yrc.strip():
        yield yrc, label


# The sites word timing is asked of, in the order they are asked. NetEase is last
# because it has not answered once: it came back with nothing for each of the seven
# songs it was tried on, while the other two answered for most of them, and asking
# it first spends a search on a site that is not going to reply. It keeps a place
# rather than being dropped -- it is the one source left that might have something
# the other two do not, and it costs nothing to have it asked last.
ONLINE_TIMING = (
    ("krc", kugou.candidates),
    ("qrc", qq.candidates),
    ("yrc", _timing_from_netease),
)
# Bumped to ask every site again: a change to how one is queried, or to what is
# made of the answer, can turn a remembered "nothing" into a hit -- and the cache
# would otherwise hide it.
TIMING_CACHE_VERSION = 3
# how long "this site has nothing for this song" is believed before it is asked
# about again
EMPTY_ANSWER_TTL_S = 3600


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


def _cache_key(track: Track) -> str | None:
    """The identity a song is remembered by.

    Title and artist only. The player reports the length as zero often enough --
    re-emitting its metadata with the field momentarily blanked -- that a cache
    keyed on it misses on the very song it just wrote, and looks the lyric and
    the timing up afresh. The length still decides which cut of a song a timing
    is taken from; it is just not part of who the song is.
    """
    if not track.title:
        return None
    return hashlib.sha1(f"{track.title}\x00{track.artist}".encode("utf-8")).hexdigest()


def _cache_path(track: Track, cache_dir: Path) -> Path | None:
    key = _cache_key(track)
    return None if key is None else cache_dir / "lyrics" / f"{key}.json"


def _timing_path(track: Track, cache_dir: Path, source: str) -> Path | None:
    """Where one site's answer about a track is remembered, empty included."""
    key = _cache_key(track)
    if key is None:
        return None
    return cache_dir / "timing" / f"{key}.{source}.v{TIMING_CACHE_VERSION}.txt"


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
    parsed = lrc.parse_with_translation(
        original, payload.get("tlrc") or "", payload.get("yrc") or ""
    )
    if parsed.lines:
        parsed.source = f"缓存: {payload.get('label') or '网易云'}"
    return parsed


def _from_online(track: Track, cache_dir: Path) -> Lyrics | None:
    try:
        result = net.lookup(track)
    except Exception:
        return None                    # the site never answered; nothing to show
    if result is None:
        return None
    original, translation, yrc, label = result
    parsed = lrc.parse_with_translation(original, translation, yrc)
    if not parsed.lines:
        return None
    parsed.source = label
    _save_cache(track, cache_dir, original, translation, yrc, label)
    return parsed


def _save_cache(
    track: Track, cache_dir: Path, original: str, translation: str, yrc: str, label: str
) -> None:
    path = _cache_path(track, cache_dir)
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"lrc": original, "tlrc": translation, "yrc": yrc, "label": label},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"PlanaLyrics: 无法写入歌词缓存 ({exc})")
