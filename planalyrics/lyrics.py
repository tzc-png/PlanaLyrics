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
from pathlib import Path

from typing import Iterable

_TS_RE = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
_META_RE = re.compile(
    r"^\[(ti|ar|al|by|re|ve|length|offset|kana|language|tool)\s*:(.*)\]$",
    re.IGNORECASE,
)
_AWLRC_RE = re.compile(r"^\[awlrc:(.*)\]\s*$")
_B64_FIELD_RE = re.compile(r",(?=lrc:|tlrc:|awlrc:)")
_CREDIT_RE = re.compile(
    r"^(作词|作曲|编曲|混音|录音|制作人|制作|Lyricist|Composer|Arranger|Producer)\s*[:：]",
    re.IGNORECASE,
)
_CJK_RE = re.compile(r"[぀-ヿ㐀-䶿一-鿿ｦ-ﾟ]")
_KANA_RE = re.compile(r"[ぁ-ゖァ-ヺー]")
_HAN_RE = re.compile(r"[㐀-䶿一-鿿]")

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
        """Only Japanese originals get a translation row.

        English and Chinese lyrics are shown on their own; a Chinese
        translation is only useful next to the Japanese it translates.
        """
        return self.script == "ja"

    def index_at(self, t_ms: int) -> int:
        """Index of the line playing at ``t_ms``, or -1 before the first one."""
        if not self._times:
            return -1
        i = bisect.bisect_right(self._times, t_ms + self.offset_ms) - 1
        return i if i >= 0 else -1


def detect_script(lines: list[Line]) -> str:
    text = "".join(line.text for line in lines)
    kana = len(_KANA_RE.findall(text))
    han = len(_HAN_RE.findall(text))
    if kana and kana >= max(3, 0.1 * han):
        return "ja"
    return "zh" if han else "latin"


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
    else:
        entries, unstamped, offset = _timed_entries(body)
        entries, translations = _split_translation_block(entries)
    return _build(entries, unstamped, offset, translations)


def parse_with_translation(lrc_text: str, tlrc_text: str = "") -> Lyrics:
    """Parse an original and its translation, as fetched separately online."""
    entries, unstamped, offset = _timed_entries(lrc_text)
    return _build(entries, unstamped, offset, _translation_map(tlrc_text))


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
) -> Lyrics:
    if not entries:
        plain = [line for line in unstamped if not _CREDIT_RE.match(line)]
        return Lyrics(lines=[Line(-1, line) for line in plain], synced=False, offset_ms=offset)

    lines = []
    for t_ms, original, translation in _drop_credits(_merge(entries)):
        if translation is None:
            translation = translations.get(t_ms)
        lines.append(Line(t_ms, original, translation or None))
    return Lyrics(lines=lines, synced=True, offset_ms=offset)


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
            unstamped.append(_clean(line))
            continue
        for times, rest in groups:
            rest = _clean(rest)
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
