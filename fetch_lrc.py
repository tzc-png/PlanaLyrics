#!/usr/bin/env python3
"""Write a .lrc with word-by-word timing beside every audio file in a folder.

    ./fetch_lrc.py <directory> [--overwrite] [--dry-run]

The folder is scanned one level deep, not recursively. For each audio file the
song is looked up by its own tags -- title and artist must both match exactly,
because a lyric that came from a different song is worse than no lyric at all --
and the first source holding word timing for it supplies the whole file.

What is written, beside the audio and named after it:

    [00:02.000]first line
    [00:02.000]first line, translated
    [00:05.500]second line
    [00:05.500]second line, translated
    [awlrc:lrc:<base64>,tlrc:<base64>,awlrc:<base64>]

Each line is followed by its Chinese translation on the same timestamp, so any
player shows both; the last line is the AMLL/Lyricify bundle with the same lyric,
its translation and the per-character timing, for players that understand it.

A lyric that comes back incomplete is discarded rather than written: it must
carry word timing, and a song that is not Chinese must carry a Chinese line. What
was skipped and why is listed at the end.

Sources are tried in this order: QQ Music, Kugou, NetEase. The first with an
exact match and word timing wins.
"""

from __future__ import annotations

import argparse
import base64
import re
import sys
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import mutagen  # noqa: E402

from planalyrics import kugou, lyrics as lrc, net, qq  # noqa: E402
from planalyrics.mpris import Track  # noqa: E402

AUDIO_SUFFIXES = (
    ".flac", ".mp3", ".m4a", ".wav", ".ogg", ".opus", ".aac", ".ape", ".wv", ".wma",
)
# how far the source's length may sit from the file's before it is a different
# recording of the same title -- a live take, an edit, a TV-size cut
LENGTH_TOLERANCE_MS = 3000

_WAVES = str.maketrans({
    "〜": "~", "～": "~",           # 〜 ～
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-",
})
_ARTIST_SEP = re.compile(r"[;/、,&]")


def norm(text) -> str:
    """A title or artist reduced to what makes two spellings the same name.

    Full-width digits and punctuation fold to half-width (the reference lyric
    writes ７ where its own audio file writes 7), the several wave dashes become
    one, case and spacing stop mattering. Nothing else is touched: 丹下桜 is not
    the same artist as グランブルーファンタジー, and this must not pretend it is.
    """
    text = unicodedata.normalize("NFKC", str(text or "")).translate(_WAVES)
    return re.sub(r"\s+", " ", text).strip().casefold()


def artist_set(text) -> frozenset[str]:
    """The people a credit names, so that "A/B" and "A;B" are the same pair."""
    return frozenset(part for part in (norm(p) for p in _ARTIST_SEP.split(str(text or ""))) if part)


def same_song(title, artist, other_title, other_artist) -> bool:
    return norm(title) == norm(other_title) and artist_set(artist) == artist_set(other_artist)


def identity(path: Path) -> tuple[str, str, int]:
    """Title, artist and length, from the tags or failing that from the name."""
    try:
        audio = mutagen.File(path)
    except Exception:
        audio = None
    title = artist = ""
    length_ms = 0
    if audio is not None:
        def first(key):
            value = audio.get(key)
            if isinstance(value, (list, tuple)):
                return str(value[0]) if value else ""
            text = getattr(value, "text", value)
            if isinstance(text, (list, tuple)):
                return str(text[0]) if text else ""
            return str(text or "")

        title, artist = first("title").strip(), first("artist").strip()
        try:
            length_ms = int(float(audio.info.length) * 1000)
        except Exception:
            length_ms = 0
    if not title or not artist:
        stem = path.stem
        if " - " in stem:
            left, right = stem.split(" - ", 1)
            title = title or right.strip()
            artist = artist or left.strip()
        else:
            title = title or stem.strip()
    return title, artist, length_ms


# -- the sources, each asked to hand over a lyric with its word timing ----


def _lyrics_from_timing(text: str, source: str):
    """A lyric built out of a timing document, which carries its own words.

    Kugou's krc and QQ's qrc hold the line and the per-character timing in the
    same place, so nothing has to be lined up against a separate lyric: each
    timed line becomes one of ours, carrying its units unchanged.
    """
    table = lrc.parse_timing(source, text)
    lines = []
    for line_ms in sorted(table):
        units = table[line_ms]
        body = "".join(unit for _offset, _duration, unit in units)
        if not body.strip():
            continue
        line = lrc.Line(line_ms, body)
        line.timelines[source] = units
        lines.append(line)
    return lrc.Lyrics(lines=lines, synced=True) if lines else None


def _from_qq(track: Track, title: str, artist: str):
    for candidate in qq._search(f"{title} {artist}", track.length_ms):
        if candidate.duration_ms and abs(candidate.duration_ms - track.length_ms) > LENGTH_TOLERANCE_MS:
            continue
        if not same_song(title, artist, candidate.title, candidate.artist):
            continue
        text = qq._fetch(candidate)
        lyrics = _lyrics_from_timing(text, "qrc") if text.strip() else None
        if lyrics is not None:
            return lyrics, f"QQ音乐: {candidate.title} - {candidate.artist}"
    return None


def _from_kugou(track: Track, title: str, artist: str):
    for candidate in kugou._asked(f"{title} {artist}", track.length_ms):
        if candidate.duration_ms and abs(candidate.duration_ms - track.length_ms) > LENGTH_TOLERANCE_MS:
            continue
        if not same_song(title, artist, candidate.title, candidate.artist):
            continue
        try:
            text = kugou._fetch(candidate)
        except Exception:
            continue
        lyrics = _lyrics_from_timing(text, "krc") if text.strip() else None
        if lyrics is not None:
            return lyrics, f"酷狗: {candidate.title} - {candidate.artist}"
    return None


def _from_netease(track: Track, title: str, artist: str):
    for candidate in net.search(f"{title} {artist}", track):
        if getattr(candidate, "duration_ms", 0) and abs(
            candidate.duration_ms - track.length_ms
        ) > LENGTH_TOLERANCE_MS:
            continue
        if not same_song(title, artist, candidate.title, candidate.artist):
            continue
        try:
            original, translation, yrc = net._lyrics(candidate.song_id)
        except Exception:
            continue
        if not yrc.strip():
            continue                     # line-level only: nothing to write as word timing
        parsed = lrc.parse_with_translation(original, translation, yrc)
        return parsed, f"网易云: {candidate.title} - {candidate.artist}"
    return None


SOURCES = (("QQ音乐", _from_qq), ("酷狗", _from_kugou), ("网易云", _from_netease))


def fetch(track: Track, title: str, artist: str):
    """The lyric and its word timing, from the first source with an exact match."""
    for name, source in SOURCES:
        try:
            found = source(track, title, artist)
        except Exception as exc:
            print(f"    {name} 查询出错：{exc}")
            continue
        if found is not None:
            return found
    return None


def add_translation(lyrics, track: Track, title: str, artist: str) -> None:
    """Fill in the Chinese translation, which only NetEase pairs with its words.

    Asked even when another site supplied the timing, under the same exact-match
    rule. The translation is lined up against the lyric by text rather than by
    time, because the two sites cut the song differently and their clocks rarely
    agree.
    """
    if any(line.translation for line in lyrics.lines):
        return
    try:
        candidates = list(net.search(f"{title} {artist}", track))
    except Exception:
        return
    for candidate in candidates:
        length = getattr(candidate, "duration_ms", 0)
        if length and abs(length - track.length_ms) > LENGTH_TOLERANCE_MS:
            continue
        if not same_song(title, artist, candidate.title, candidate.artist):
            continue
        try:
            original, translation, _yrc = net._lyrics(candidate.song_id)
        except Exception:
            return
        if not translation.strip():
            continue
        paired = lrc.parse_with_translation(original, translation, "")
        _map_translations_by_text(lyrics, paired.lines)
        return


def _map_translations_by_text(lyrics, source_lines) -> None:
    """Put NetEase's per-line translation onto our lines by lining the texts up.

    The translation arrives paired with NetEase's own original, while our lyric
    came from QQ or Kugou, so the two cut the song differently and their clocks
    rarely agree. Matching by timestamp reaches only the lines that happen to
    land within a few hundred milliseconds of each other; lining the two
    originals up character by character survives both the different cut and the
    different clock. A line that lines up with nothing -- a credit line one
    source carries but the other does not -- is left untranslated.
    """
    our_chars: list[str] = []
    our_line: list[int] = []
    for index, line in enumerate(lyrics.lines):
        for char in line.text:
            our_chars.append(char)
            our_line.append(index)

    their_chars: list[str] = []
    their_line: list[int] = []
    for index, line in enumerate(source_lines):
        for char in line.text:
            their_chars.append(char)
            their_line.append(index)

    if not our_chars or not their_chars:
        return

    matcher = SequenceMatcher(None, our_chars, their_chars, autojunk=False)
    aligned: dict[int, dict[int, int]] = {}
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "equal":
            continue
        for step in range(i2 - i1):
            ours = our_line[i1 + step]
            theirs = their_line[j1 + step]
            aligned.setdefault(ours, {})
            aligned[ours][theirs] = aligned[ours].get(theirs, 0) + 1

    for ours, counts in aligned.items():
        theirs = max(counts, key=counts.get)
        line = lyrics.lines[ours]
        translation = source_lines[theirs].translation
        if translation and counts[theirs] * 2 >= len(line.text):
            line.translation = translation


# -- serialising ------------------------------------------------------


def stamp(ms: float) -> str:
    ms = max(0, int(round(ms)))
    return f"[{ms // 60000:02d}:{ms % 60000 / 1000:06.3f}]"


def units_of(line) -> list[tuple[int, int, str]] | None:
    """The line's word timing, whichever source it came from."""
    for key in ("awlrc", "yrc", "krc", "qrc", "enhanced"):
        units = line.timelines.get(key)
        if units:
            return units
    return None


def _missing(lyrics) -> str | None:
    """Why this lyric is not worth writing, or None when it is complete.

    A file without word timing, or a foreign song left with no Chinese line, is
    worse than no file at all, so it is discarded rather than written.
    """
    if not any(units_of(line) for line in lyrics.lines):
        return "没有逐字时间轴"
    if lyrics.script != "zh" and not any(line.translation for line in lyrics.lines):
        return "没有中文译文"
    return None


def build_document(lyrics) -> str:
    plain, translated, timed, visible = [], [], [], []
    for line in lyrics.lines:
        if line.t_ms < 0 or not line.text.strip():
            continue
        plain.append(f"{stamp(line.t_ms)}{line.text}")
        visible.append(f"{stamp(line.t_ms)}{line.text}")
        if line.translation:
            translated.append(f"{stamp(line.t_ms)}{line.translation}")
            visible.append(f"{stamp(line.t_ms)}{line.translation}")
        units = units_of(line)
        if units:
            body = "".join(
                f"<{offset},{duration}>{text}" for offset, duration, text in units
            )
            timed.append(f"{stamp(line.t_ms)}{body}")

    fields = [f"lrc:{base64.b64encode(chr(10).join(plain).encode()).decode()}"]
    if translated:
        fields.append(f"tlrc:{base64.b64encode(chr(10).join(translated).encode()).decode()}")
    if timed:
        fields.append(f"awlrc:{base64.b64encode(chr(10).join(timed).encode()).decode()}")

    return "\n".join(visible) + f"\n[awlrc:{','.join(fields)}]\n"


def write(path: Path, lyrics) -> tuple[bool, str]:
    """Write the .lrc beside the audio; says whether it carries word timing."""
    document = build_document(lyrics)
    with_word = "[awlrc:lrc:" in document and ",awlrc:" in document
    with open(path, "w", encoding="utf-8-sig", newline="\n") as handle:
        handle.write(document)
    return with_word, document


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("directory", help="要扫描的目录（只处理这一层，不进子目录）")
    parser.add_argument("--overwrite", action="store_true", help="覆盖已存在的 .lrc")
    parser.add_argument("--dry-run", action="store_true", help="只查找和匹配，不写文件")
    args = parser.parse_args(argv)

    folder = Path(args.directory).expanduser()
    if not folder.is_dir():
        print(f"不是目录：{folder}", file=sys.stderr)
        return 2

    files = sorted(
        p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in AUDIO_SUFFIXES
    )
    if not files:
        print(f"{folder} 下没有音频文件")
        return 0
    print(f"{folder}：{len(files)} 个音频文件\n")

    written, skipped, failed = 0, 0, []
    for index, audio in enumerate(files, 1):
        target = audio.with_suffix(".lrc")
        head = f"[{index}/{len(files)}] {audio.name}"
        if target.exists() and not args.overwrite:
            print(f"{head}\n    已有 .lrc，跳过（--overwrite 可覆盖）")
            skipped += 1
            continue

        title, artist, length_ms = identity(audio)
        if not title or not artist:
            print(f"{head}\n    读不出标题/艺术家，跳过")
            failed.append((audio.name, "读不出标题或艺术家"))
            continue

        track = Track(title=title, artist=artist, length_ms=length_ms)
        found = fetch(track, title, artist)
        if found is None:
            print(f"{head}\n    没有严格匹配的源")
            failed.append((audio.name, "没有标题+艺术家都一致的源"))
            continue

        lyrics, label = found
        if args.dry_run:
            print(f"{head}\n    匹配：{label}")
            written += 1
            continue

        add_translation(lyrics, track, title, artist)
        reason = _missing(lyrics)
        if reason:
            print(f"{head}\n    舍弃：{reason}")
            failed.append((audio.name, reason))
            continue
        try:
            with_word, _doc = write(target, lyrics)
        except OSError as exc:
            print(f"{head}\n    写入失败：{exc}")
            failed.append((audio.name, f"写入失败：{exc}"))
            continue

        kind = "逐字轴" if with_word else "仅行级歌词"
        print(f"{head}\n    匹配：{label}\n    写出 {target.name}（{kind}，{len(lyrics.lines)} 行）")
        written += 1

    print(f"\n完成：写出 {written}，跳过 {skipped}，失败 {len(failed)}")
    if failed:
        print("\n失败清单：")
        for name, why in failed:
            print(f"  {name}\n      {why}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
