"""LRC parsing.

Written against the two formats actually present in this machine's library:

* plain ``[mm:ss.xx]`` sidecar files, including the empty/real duplicate-line
  pairs that wav2lrc emits;
* the messy ``LYRICS`` tag embedded in some FLACs -- mixed 1/2/3 decimal
  places, leading credit lines, a translation block whose timestamps restart
  from zero, and a trailing ``[awlrc:...]`` base64 bundle.
"""

from __future__ import annotations

import base64
import bisect
import codecs
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

from typing import Iterable

_TS_RE = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
_META_RE = re.compile(
    r"^\[(ti|ar|al|by|re|ve|length|offset|kana|language|tool)\s*:(.*)\]$",
    re.IGNORECASE,
)
_AWLRC_RE = re.compile(r"^\[awlrc:(.*)\]\s*$")
# three shapes of word-level timing, told apart by how a unit is written:
_WORD_RE = re.compile(r"<(\d+),(\d+)>([^<]*)")                  # awlrc: <offset,dur>text
_ENHANCED_RE = re.compile(r"<(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?>")   # enhanced LRC: <mm:ss.xx>
_YRC_LINE_RE = re.compile(r"^\[(\d+),(\d+)\](.*)$")             # yrc: [start,dur](start,dur,0)text
_YRC_WORD_RE = re.compile(r"\((\d+),(\d+),\d+\)([^(]*)")
_KRC_LINE_RE = re.compile(r"^\[(\d+),(\d+)\](.*)$")             # krc: [start,dur]<start,dur,0>text
_KRC_WORD_RE = re.compile(r"<(\d+),(\d+),\d+>([^<]*)")
# the marks on their own, for taking them back out of text that is to be read
_MARK_RE = re.compile(r"<\d+,\d+>|<\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?>")
_B64_FIELD_RE = re.compile(r",(?=lrc:|tlrc:|awlrc:)")
_CREDIT_RE = re.compile(
    r"^(作词|作曲|编曲|混音|录音|制作人|制作|Lyricist|Composer|Arranger|Producer)\s*[:：]",
    re.IGNORECASE,
)
_CJK_RE = re.compile(r"[぀-ヿ㐀-䶿一-鿿ｦ-ﾟ]")
_HAN_RE = re.compile(r"[㐀-䶿一-鿿]")
_LATIN_RE = re.compile(r"[A-Za-z]")
# Scripts that are neither Chinese nor Latin: Japanese, Korean, and the rest of
# the non-Latin world. A song written in one of these is what gets a Chinese row.
_FOREIGN_RE = re.compile(
    "["
    "Ͱ-Ͽ"      # Greek
    "Ѐ-ԯ"      # Cyrillic
    "԰-֏"      # Armenian
    "֐-׿"      # Hebrew
    "؀-ۿ"      # Arabic
    "ऀ-ॿ"      # Devanagari
    "฀-๿"      # Thai
    "぀-ヿ"      # kana
    "ᄀ-ᇿ㄰-㆏가-힣"   # Hangul
    "]"
)

_BOMS = (
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)


@dataclass
class Line:
    t_ms: int
    text: str
    translation: str | None = None
    # Word-level timings for this line, by where each came from: one
    # ``(offset_ms, duration_ms, text)`` per unit, offsets from the line's start.
    # A lyric can offer more than one, and the overlay picks among them by the
    # settings; empty means the sweep has only the average pace to go by.
    timelines: dict[str, list[tuple[int, int, str]]] = field(default_factory=dict)


@dataclass
class Lyrics:
    lines: list[Line] = field(default_factory=list)
    synced: bool = False
    source: str = ""
    offset_ms: int = 0
    script: str = ""
    _times: list[int] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        if self.lines and all(line.t_ms >= 0 for line in self.lines):
            self.lines.sort(key=lambda line: line.t_ms)
            self.synced = True
        if self.synced:
            self._times = [line.t_ms for line in self.lines]
        if not self.script:
            self.script = detect_script(self.lines)

    @property
    def wants_translation(self) -> bool:
        """A Chinese row goes with any song that is neither Chinese nor English.

        Japanese, Korean and every other language are foreign enough that a
        Chinese translation is worth showing; Chinese needs no translation of
        itself, and English is not what this setting is for.
        """
        return self.script == "other"

    def index_at(self, t_ms: int) -> int:
        """Index of the line playing at ``t_ms``, or -1 before the first one."""
        if not self._times:
            return -1
        i = bisect.bisect_right(self._times, t_ms + self.offset_ms) - 1
        return i if i >= 0 else -1


def detect_script(lines: list[Line]) -> str:
    """Which of the three kinds of song this is: ``zh``, ``en`` or ``other``.

    Only one distinction is really being made -- Chinese and English lyrics stand
    on their own, everything else is shown with a Chinese row -- so the point is
    to tell those two from the rest, not from each other. Latin script counts as
    English, which is what nearly every Latin-script song in a collection like
    this is; Japanese, Korean and the other non-Latin scripts are ``other``.
    """
    text = "".join(line.text for line in lines)
    han = len(_HAN_RE.findall(text))
    foreign = len(_FOREIGN_RE.findall(text))
    # A couple of foreign characters inside otherwise Chinese text -- a stray
    # katakana lengthener, say -- are not enough to call the whole song foreign.
    if foreign >= max(2, 0.1 * han):
        return "other"
    if han:
        return "zh"
    if _LATIN_RE.search(text):
        return "en"
    return "other"


def decode_bytes(data: bytes) -> tuple[str, str]:
    for bom, encoding in _BOMS:
        if data.startswith(bom):
            return data.decode(encoding, "replace"), encoding
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("gb18030"), "gb18030"
    except UnicodeDecodeError:
        return data.decode("latin-1"), "latin-1"


def parse_file(path: str | Path) -> Lyrics:
    return parse(decode_bytes(Path(path).read_bytes())[0])


def parse(text: str) -> Lyrics:
    bundle, body = _split_bundle(text)

    if bundle.get("lrc", "").strip():
        entries, unstamped, offset = _timed_entries(bundle["lrc"])
        translations = _translation_map(bundle.get("tlrc", ""))
        timelines = {
            "awlrc": _awlrc_timeline(bundle.get("awlrc", "")),
            "enhanced": _enhanced_timeline(bundle["lrc"]),
        }
    else:
        entries, unstamped, offset = _timed_entries(body)
        entries, translations = _split_translation_block(entries)
        timelines = {
            "awlrc": _awlrc_timeline(body),
            "enhanced": _enhanced_timeline(body),
        }
    return _build(entries, unstamped, offset, translations, timelines)


def parse_with_translation(lrc_text: str, tlrc_text: str = "", yrc_text: str = "") -> Lyrics:
    """Parse an original, its translation, and the word timing fetched with them."""
    entries, unstamped, offset = _timed_entries(lrc_text)
    timelines = {
        "awlrc": _awlrc_timeline(lrc_text),
        "enhanced": _enhanced_timeline(lrc_text),
        "yrc": _yrc_timeline(yrc_text),
    }
    return _build(entries, unstamped, offset, _translation_map(tlrc_text), timelines)


def _translation_map(text: str) -> dict[int, str]:
    """One translation per timestamp, preferring the one that translates.

    A source can carry several translations of the same lines -- a file holding
    both Chinese and a romanisation, or a player handing over both -- and they
    share the original's timestamps. A romanisation is not a translation, so
    where the two collide the Chinese line is the one to keep.
    """
    out: dict[int, str] = {}
    for t_ms, value in _timed_entries(text)[0]:
        value = value.strip()
        if not value:
            continue
        current = out.get(t_ms)
        if current is None or (_HAN_RE.search(value) and not _HAN_RE.search(current)):
            out[t_ms] = value
    return out


def _build(
    entries: list[tuple[int, str]],
    unstamped: list[str],
    offset: int,
    translations: dict[int, str],
    timelines: dict[str, dict[int, list[tuple[int, int, str]]]] | None = None,
) -> Lyrics:
    if not entries:
        plain = [line for line in unstamped if not _CREDIT_RE.match(line)]
        return Lyrics(lines=[Line(-1, line) for line in plain], synced=False, offset_ms=offset)

    lines = []
    for t_ms, original, translation in _drop_credits(_merge(entries)):
        if translation is None:
            translation = translations.get(t_ms)
        lines.append(
            Line(t_ms, original, translation or None, _timelines_for(timelines, t_ms, original))
        )
    return Lyrics(lines=lines, synced=True, offset_ms=offset)


def _timelines_for(
    timelines: dict[str, dict[int, list[tuple[int, int, str]]]] | None, t_ms: int, text: str
) -> dict[str, list[tuple[int, int, str]]]:
    """Which of the offered timings can be placed on this line.

    A timing that does not spell the line out character for character cannot be
    put on its glyphs, so it is dropped here rather than lighting up the wrong
    characters: the sweep then falls through to the next source, or to the
    average pace.
    """
    kept: dict[str, list[tuple[int, int, str]]] = {}
    for source, table in (timelines or {}).items():
        units = table.get(t_ms) or _nearest_units(table, t_ms)
        if _fits(units, text):
            kept[source] = units
    return kept


def _nearest_units(table: dict[int, list[tuple[int, int, str]]], t_ms: int, tolerance_ms: int = 700):
    """The timing nearest ``t_ms``, if one is close enough to be the same line.

    A timing written alongside the lyric shares its timestamps exactly, but one
    fetched separately -- NetEase's yrc -- is timed independently and lands a few
    hundred milliseconds away. Matching the words, as the caller does, is what
    keeps a near miss from being taken for a different line.
    """
    best = None
    for key, units in table.items():
        delta = abs(key - t_ms)
        if delta <= tolerance_ms and (best is None or delta < best[0]):
            best = (delta, units)
    return None if best is None else best[1]


def _stamped_lines(text: str):
    """``(line_ms, rest)`` for each line that begins with a timestamp."""
    for raw in text.splitlines():
        line = raw.strip()
        match = _TS_RE.match(line)
        if match is not None:
            yield _stamp_ms(match), line[match.end():]


def _awlrc_timeline(text: str) -> dict[int, list[tuple[int, int, str]]]:
    """Timing from an AMLL/Lyricify bundle's ``awlrc`` field.

    ``<offset,duration>`` units written after each line's timestamp, the offsets
    measured from that line's start.
    """
    out: dict[int, list[tuple[int, int, str]]] = {}
    for line_ms, rest in _stamped_lines(text):
        units = [
            (int(offset), int(duration), unit)
            for offset, duration, unit in _WORD_RE.findall(rest)
        ]
        if units:
            out[line_ms] = units
    return out


def _enhanced_timeline(text: str) -> dict[int, list[tuple[int, int, str]]]:
    """Timing from the inline ``<mm:ss.xx>`` marks of an enhanced LRC.

    Those marks are absolute, so each unit runs until the next mark; the line's
    last unit runs to the next line, and the song's last line to the end.
    """
    per_line: list[tuple[int, list[tuple[int, str]]]] = []
    for line_ms, rest in _stamped_lines(text):
        marks = list(_ENHANCED_RE.finditer(rest))
        units = []
        for index, mark in enumerate(marks):
            end = marks[index + 1].start() if index + 1 < len(marks) else len(rest)
            unit = rest[mark.end():end]
            if unit:
                units.append((_stamp_ms(mark), unit))
        if units:
            per_line.append((line_ms, units))

    out: dict[int, list[tuple[int, int, str]]] = {}
    for index, (line_ms, units) in enumerate(per_line):
        line_end = per_line[index + 1][0] if index + 1 < len(per_line) else 0
        timed = []
        for position, (start, unit) in enumerate(units):
            if position + 1 < len(units):
                duration = units[position + 1][0] - start
            else:
                duration = line_end - start
            timed.append((max(0, start - line_ms), max(0, duration), unit))
        out[line_ms] = timed
    return out


def parse_timing(source: str, text: str) -> dict[int, list[tuple[int, int, str]]]:
    """Read a timing document, picking the shape by which site wrote it."""
    reader = {"yrc": _yrc_timeline, "krc": _krc_timeline}.get(source)
    return reader(text) if reader else {}


def attach_timelines(lyrics: "Lyrics", source: str, table: dict) -> int:
    """Put a separately fetched timing onto a lyric already parsed.

    Returns how many lines it could be placed on. The same rule the parse-time
    sources go by applies here, and matters more: a timing fetched from another
    site is timed independently, so a near line is accepted when the words match
    and a line whose words differ is left to the average pace.
    """
    placed = 0
    for line in lyrics.lines:
        units = table.get(line.t_ms) or _nearest_units(table, line.t_ms)
        if _fits(units, line.text):
            line.timelines[source] = units
            placed += 1
    return placed


def _fits(units, text: str) -> bool:
    """Whether a timing can be placed on a line.

    It has to spell the line out character for character, or it would light up
    the wrong characters; and it has to carry a duration somewhere, or it says
    nothing about pace -- which is what an enhanced LRC's last line amounts to,
    having no next line to run up to.
    """
    return bool(units) and any(unit[1] > 0 for unit in units) and (
        "".join(unit[2] for unit in units).strip() == text.strip()
    )


def align_timeline(lyrics: "Lyrics", table: dict, source: str, min_match: float = 0.6) -> int:
    """Lay a timing from elsewhere over the lyric by lining the two texts up.

    A timing fetched from another site is not only clocked independently, it is
    usually cut into different lines -- one site breaks a sentence where the
    lyric breaks a phrase -- and its words can differ a little, a syllable
    written out where the lyric uses the character for it. Matching line against
    line therefore finds nothing at all, so the whole lyric is lined up against
    the whole timing character by character instead, and each character takes the
    time of the character it lines up with. Returns how many lines it covered, or
    0 -- attaching nothing -- when the two have too little in common to be the
    same song, which is what keeps a wrong match from lighting the wrong words.
    """
    timing: list[tuple[str, float, float]] = []
    for line_ms in sorted(table):
        for offset, duration, text in table[line_ms]:
            start = line_ms + offset
            for index, char in enumerate(text):
                timing.append((char, start + duration * index / len(text),
                               start + duration * (index + 1) / len(text)))
    lyric_text = "".join(line.text for line in lyrics.lines)
    if not timing or not lyric_text:
        return 0

    matcher = SequenceMatcher(None, lyric_text, "".join(item[0] for item in timing), autojunk=False)
    when: list[tuple[float, float] | None] = [None] * len(lyric_text)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "equal":
            continue
        for step in range(i2 - i1):
            when[i1 + step] = (timing[j1 + step][1], timing[j1 + step][2])

    lined_up = sum(1 for item in when if item is not None)
    if lined_up < min_match * len(lyric_text):
        return 0
    _fill_gaps(when)
    return _attach_chars(lyrics, when, source)


def _fill_gaps(when: list) -> None:
    """Give the characters that lined up with nothing a time of their own.

    A character the other text spells differently is still sung, so it takes a
    share of the time between the characters either side of it -- the closest the
    two texts can come to agreeing.
    """
    known = [index for index, item in enumerate(when) if item is not None]
    if not known:
        return
    for index in range(known[0]):
        when[index] = when[known[0]]
    for index in range(known[-1] + 1, len(when)):
        when[index] = when[known[-1]]
    for left, right in zip(known, known[1:]):
        if right - left <= 1:
            continue
        begin, end = when[left][1], when[right][0]
        for step in range(1, right - left):
            when[left + step] = (
                begin + (end - begin) * step / (right - left),
                begin + (end - begin) * (step + 1) / (right - left),
            )


def _attach_chars(lyrics: "Lyrics", when: list, source: str) -> int:
    """Cut a run of per-character times into one line's worth of units each."""
    placed, cursor = 0, 0
    for line in lyrics.lines:
        chars = when[cursor:cursor + len(line.text)]
        cursor += len(line.text)
        units = [
            (max(0, round(item[0] - line.t_ms)), max(0, round(item[1] - item[0])), char)
            for char, item in zip(line.text, chars)
            if item is not None
        ]
        if units and any(unit[1] > 0 for unit in units) and (
            "".join(unit[2] for unit in units) == line.text
        ):
            line.timelines[source] = units
            placed += 1
    return placed


def _krc_timeline(text: str) -> dict[int, list[tuple[int, int, str]]]:
    """Timing from Kugou's krc, whose unit offsets count from the line's start."""
    out: dict[int, list[tuple[int, int, str]]] = {}
    for raw in text.splitlines():
        match = _KRC_LINE_RE.match(raw.strip())
        if match is None:
            continue
        units = [
            (int(offset), int(duration), unit)
            for offset, duration, unit in _KRC_WORD_RE.findall(match.group(3))
        ]
        if units:
            out[int(match.group(1))] = units
    return out


def _yrc_timeline(text: str) -> dict[int, list[tuple[int, int, str]]]:
    """Timing from NetEase's ``yrc``, whose unit starts are absolute."""
    out: dict[int, list[tuple[int, int, str]]] = {}
    for raw in text.splitlines():
        match = _YRC_LINE_RE.match(raw.strip())
        if match is None:
            continue
        line_ms = int(match.group(1))
        units = [
            (max(0, int(start) - line_ms), int(duration), unit)
            for start, duration, unit in _YRC_WORD_RE.findall(match.group(3))
        ]
        if units:
            out[line_ms] = units
    return out


def _stamp_ms(match: re.Match) -> int:
    fraction = match.group(3)
    millis = int(fraction.ljust(3, "0")) if fraction else 0
    return int(match.group(1)) * 60_000 + int(match.group(2)) * 1_000 + millis


def _segments(line: str) -> list[tuple[list[int], str]]:
    """``(timestamps, text)`` groups on one line.

    Timestamps normally sit at the front and the text follows. One can also turn
    up after text: two blocks written without a newline between them do exactly
    that, and stopping at the first stamp would swallow the whole following block
    into this line's text.
    """
    groups: list[tuple[list[int], str]] = []
    times: list[int] = []
    start = None
    for match in _TS_RE.finditer(line):
        if start is not None:
            between = line[start : match.start()]
            # stamps with nothing between them all belong to the text that
            # follows them, which is how a repeated line is written
            if between.strip():
                groups.append((times, between))
                times = []
        times.append(_stamp_ms(match))
        start = match.end()
    if times:
        groups.append((times, line[start:]))
    return groups


def _strip_marks(text: str) -> str:
    """Take inline word-timing marks back out of text that is meant to be read.

    They are timing, not lyric, and a line that kept them would print them.
    """
    return _MARK_RE.sub("", text)


def _clean(text: str) -> str:
    """Drop the tail of a line once the tag's bytes stopped decoding.

    U+FFFD is a byte the decoder could not read, which in practice means the
    field was assembled from pieces and the seam landed inside a character.
    Whatever follows is the next block's business, not this line's text.
    """
    return text.split("\ufffd", 1)[0].strip()


def _timed_entries(text: str) -> tuple[list[tuple[int, str]], list[str], int]:
    entries: list[tuple[int, str]] = []
    unstamped: list[str] = []
    offset = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        groups = _segments(line)
        if not groups:
            meta = _META_RE.match(line)
            if meta:
                if meta.group(1).lower() == "offset":
                    offset = _to_int(meta.group(2), offset)
                continue
            unstamped.append(_strip_marks(_clean(line)))
            continue
        for times, rest in groups:
            rest = _strip_marks(_clean(rest))
            if rest:
                meta = _META_RE.match(rest)
                if meta:
                    if meta.group(1).lower() == "offset":
                        offset = _to_int(meta.group(2), offset)
                    continue
            for t_ms in times:
                entries.append((t_ms, rest))
    return entries, unstamped, offset


def _merge(entries: Iterable[tuple[int, str]]) -> list[tuple[int, str, str | None]]:
    """Drop empty texts and coalesce repeated timestamps.

    wav2lrc writes each line twice -- an empty one then the real one. When two
    distinct texts share a timestamp and differ in script, the second is the
    translation; otherwise they are joined.
    """
    order: list[int] = []
    groups: dict[int, list[str]] = {}
    for t_ms, text in entries:
        text = text.strip()
        if not text:
            continue
        if t_ms not in groups:
            groups[t_ms] = []
            order.append(t_ms)
        groups[t_ms].append(text)

    rows: list[tuple[int, str, str | None]] = []
    for t_ms in order:
        texts = groups[t_ms]
        original, translation = texts[0], None
        if len(texts) == 2 and _mostly_cjk(texts[0]) != _mostly_cjk(texts[1]):
            translation = texts[1]
        elif len(texts) > 1:
            original = " ".join(texts)
        rows.append((t_ms, original, translation))
    return rows


def _split_translation_block(
    entries: list[tuple[int, str]],
) -> tuple[list[tuple[int, str]], dict[int, str]]:
    """Detect a translation block, which restarts its timestamps from zero.

    Sorting can never find this boundary, so it is its own pass. All three
    signals are required: a backward step in the timestamps, heavy reuse of the
    original block's timestamps, and (as a tiebreak) a script change.
    """
    if len(entries) < 4:
        return entries, {}

    cut = None
    for i in range(1, len(entries)):
        if entries[i][0] < entries[i - 1][0]:
            cut = i
            break
    if cut is None:
        return entries, {}

    head, tail = entries[:cut], entries[cut:]
    head_times = {t_ms for t_ms, _ in head}
    reuse = sum(1 for t_ms, _ in tail if t_ms in head_times) / len(tail)
    script_changed = not _mostly_cjk("".join(text for _, text in head)) and _mostly_cjk(
        "".join(text for _, text in tail)
    )

    if reuse >= 0.6 or (reuse >= 0.3 and script_changed):
        return head, {t_ms: text for t_ms, text in tail}
    return entries, {}


def _drop_credits(rows: list[tuple[int, str, str | None]]) -> list[tuple[int, str, str | None]]:
    kept: list[tuple[int, str, str | None]] = []
    leading = True
    for row in rows:
        if leading and _CREDIT_RE.match(row[1]):
            continue
        leading = False
        kept.append(row)
    return kept


def _split_bundle(text: str) -> tuple[dict[str, str], str]:
    bundle: dict[str, str] = {}
    kept: list[str] = []
    for raw in text.splitlines():
        match = _AWLRC_RE.match(raw.strip())
        if not match:
            kept.append(raw)
            continue
        if not bundle:
            bundle = _decode_bundle(match.group(1))
    return bundle, "\n".join(kept)


def _decode_bundle(payload: str) -> dict[str, str]:
    decoded: dict[str, str] = {}
    for chunk in _B64_FIELD_RE.split(payload):
        key, sep, value = chunk.partition(":")
        if not sep:
            continue
        try:
            decoded[key.strip()] = base64.b64decode(value.strip(), validate=False).decode(
                "utf-8", "replace"
            )
        except (ValueError, base64.binascii.Error):
            continue
    return decoded


def _mostly_cjk(text: str) -> bool:
    letters = [char for char in text if not char.isspace()]
    if not letters:
        return False
    return sum(1 for char in letters if _CJK_RE.match(char)) / len(letters) >= 0.5


def _to_int(value: str, default: int) -> int:
    try:
        return int(float(value.strip()))
    except ValueError:
        return default
