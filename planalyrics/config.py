"""The settings file, and the defaults it is seeded from.

config.default.toml, shipped beside the source, holds the defaults. It is only
ever read: it seeds ~/.config/PlanaLyrics/config.toml on first run, and the
menu's "restore defaults" copies it back over whatever is there. Nothing the app
does writes to it.

config.toml is the only file the app writes, and it stays hand-editable. The app
edits it a line at a time rather than regenerating it, so the comments
explaining every setting stay where they are. Dragging the window around is not
a setting and is not written anywhere: every run opens the window the way this
file says.
"""

from __future__ import annotations

import json
import re
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from gi.repository import Gio

CONFIG_DIR = Path.home() / ".config" / "PlanaLyrics"
CONFIG_PATH = CONFIG_DIR / "config.toml"
CACHE_DIR = Path.home() / ".cache" / "PlanaLyrics"
DEFAULTS_PATH = Path(__file__).resolve().parent.parent / "config.default.toml"
# A note the desktop icon's actions leave for the run in progress. Restoring the
# defaults from the icon has to reach a window another process owns, and that
# window may be sitting somewhere the clicker cannot reach, so the file cannot be
# the only channel.
RESET_REQUEST = CONFIG_DIR / "reset-window"

# The closed sets and shapes the settings file may name. Kept here so a wrong
# value can be recognised while reading; overlay.py holds the same lists keyed to
# what it does with them, and a test keeps the two in step.
TYPE_HINTS = ("normal", "utility", "dock", "notification")
FONT_WEIGHTS = ("thin", "light", "normal", "medium", "semibold", "bold", "heavy", "black")
COLOUR_KEYS = (
    "border_color", "glow_color", "color_current", "color_lyrics", "color_translation",
    "halo_color", "light_color_current", "light_color_lyrics", "light_color_translation",
    "light_halo_color",
)
_COLOUR_RE = re.compile(r"^#?(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")

# whether the last read of the settings file had to put the defaults back, so the
# app can say so rather than letting a wipe happen quietly
_last_load_restored = False

# when the app last wrote config.toml itself, so the file monitor can tell that
# apart from the user saving the file and leave it alone
_self_write = 0.0


@dataclass
class PlayerConfig:
    pin: str = ""
    lxmusic_port: int = 23330
    poll_ms: int = 500


@dataclass
class WindowConfig:
    # the top-left corner: a default that still fits whatever screen it lands on
    x: int = 0
    y: int = 0
    type_hint: str = "normal"
    show_in_taskbar: bool = True
    panel_width: int = 578
    panel_height: int = 287
    min_panel_width: int = 320
    min_panel_height: int = 120
    corner_radius: int = 14
    border_width: int = 2
    border_color: str = "#FFFFFF8C"
    glow_size: int = 10
    glow_color: str = "#00000059"
    anchor_ratio: float = 0.5
    context_lines: int = 1
    context_opacity: float = 0.2
    context_scale: float = 0.75
    blur_px: int = 5
    karaoke: bool = True
    unsung_opacity: float = 0.35
    karaoke_tail: float = 0.88
    karaoke_max_ms: int = 6000
    karaoke_lift: int = 4
    karaoke_lift_span: float = 1.4
    font_family: str = "Noto Sans CJK SC"
    font_weight: str = "black"
    font_lyrics: int = 34
    font_translation: int = 23
    font_scale: int = 100
    line_spacing: int = 24
    inner_spacing: int = 3
    padding: int = 28
    edge_fade: int = 14
    color_current: str = "#FFFFFFFF"
    color_lyrics: str = "#FFFFFFFF"
    color_translation: str = "#FFFFFFA6"
    halo_color: str = "#000000"
    halo_alpha: float = 0.9
    halo_radius: int = 0
    adapt_to_background: bool = True
    light_color_current: str = "#0A0A0AFF"
    light_color_lyrics: str = "#0A0A0AFF"
    light_color_translation: str = "#5A6270FF"
    light_halo_color: str = "#FFFFFF"
    animation_ms: int = 500
    show_translation: bool = True


@dataclass
class LyricsConfig:
    music_dirs: list[str] = field(default_factory=list)
    cache_dir: str = ""
    online_enabled: bool = True
    search_translation: bool = True
    offset_ms: int = 0

    @property
    def resolved_cache_dir(self) -> Path:
        return Path(self.cache_dir).expanduser() if self.cache_dir else CACHE_DIR


@dataclass
class Config:
    player: PlayerConfig = field(default_factory=PlayerConfig)
    window: WindowConfig = field(default_factory=WindowConfig)
    lyrics: LyricsConfig = field(default_factory=LyricsConfig)


def load(path: Path = CONFIG_PATH) -> Config:
    """Read the settings file, or put the shipped defaults back if it is unusable.

    Only config.toml is read. Where the window was dragged to and how big it was
    dragged are this session's business: they are not written anywhere, so every
    run opens the window the way the settings file describes. Dragging is for
    trying a shape out; "save current window size" in the menu is what keeps one.

    A file edited by hand can say things that cannot be meant -- a fraction of
    50, a panel narrower than its own margins, a colour that is not a colour --
    and the result would be a window nobody can read or click. Since editing the
    file is no longer the ordinary way to change a setting, the everyday cost of
    being strict about it is small, and the file that was there is kept beside
    the defaults it is replaced with.
    """
    global _last_load_restored
    _last_load_restored = False
    if not path.exists():
        seed(path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return _put_defaults_back(path, [f"读不出来：{exc}"])

    config = Config()
    _apply(config.player, data.get("player"))
    _apply(config.window, data.get("window"))
    _apply(config.lyrics, data.get("lyrics"))

    problems = _problems(config)
    if problems:
        return _put_defaults_back(path, problems)
    return config


def take_restored_flag() -> bool:
    """True once if the last read had to put the defaults back."""
    global _last_load_restored
    restored, _last_load_restored = _last_load_restored, False
    return restored


def _put_defaults_back(path: Path, problems: list[str]) -> Config:
    """Say what was wrong, then put the defaults back and keep the old file."""
    for problem in problems:
        print(f"PlanaLyrics: 设置有问题 —— {problem}")
    if not restore_defaults(path):
        return Config()
    print(f"PlanaLyrics: 已恢复默认设置，原文件保存为 {path.name}.bak")
    global _last_load_restored
    _last_load_restored = True
    return Config()


def _problems(config: Config) -> list[str]:
    """Everything in these settings that cannot be meant.

    Deliberately narrow: a value that is merely unusual is not a problem, so a
    colour nobody would pick, a tiny panel, or a translation font larger than the
    lyric font all pass. What fails is the impossible -- something of the wrong
    kind, a fraction outside its range, or a panel whose own margins leave no
    room for the text.
    """
    found: list[str] = []
    for section in (config.player, config.window, config.lyrics):
        for name, value in vars(section).items():
            if name.startswith("_") or value is None:
                continue
            expected = type(getattr(type(section)(), name))
            if _wrong_kind(expected, value):
                found.append(f"{name} = {value!r} 不是{_kind_name(expected)}")
    if found:
        # comparing a value of the wrong kind would be the next mistake
        return found

    window, lyrics = config.window, config.lyrics
    for name in ("anchor_ratio", "context_opacity", "unsung_opacity", "halo_alpha"):
        if not 0.0 <= getattr(window, name) <= 1.0:
            found.append(f"{name} = {getattr(window, name)} 应该是一个 0~1 的比例")
    if not 0.05 <= window.karaoke_tail <= 1.0:
        found.append(f"karaoke_tail = {window.karaoke_tail} 应该是一个 0~1 的比例")
    if window.context_scale < 0.05:
        found.append(f"context_scale = {window.context_scale} 太小，前后句会缩到看不见")
    if window.karaoke_lift_span < 0.05:
        found.append(f"karaoke_lift_span = {window.karaoke_lift_span} 太小")

    for name in ("panel_width", "panel_height", "min_panel_width", "min_panel_height",
                 "font_lyrics", "font_translation", "font_scale", "karaoke_max_ms"):
        if getattr(window, name) < 1:
            found.append(f"{name} = {getattr(window, name)} 应该大于 0")
    if config.player.poll_ms < 1:
        found.append(f"poll_ms = {config.player.poll_ms} 应该大于 0")

    for name in ("context_lines", "blur_px", "glow_size", "corner_radius", "border_width",
                 "halo_radius", "line_spacing", "inner_spacing", "padding", "edge_fade",
                 "karaoke_lift", "animation_ms"):
        if getattr(window, name) < 0:
            found.append(f"{name} = {getattr(window, name)} 不能是负数")
    if config.player.lxmusic_port < 1:
        found.append(f"lxmusic_port = {config.player.lxmusic_port} 应该是一个端口号")

    if window.padding * 2 >= window.panel_width:
        found.append(f"padding = {window.padding} 太大，左右留白加起来超过了面板宽度")
    if window.min_panel_width > window.panel_width:
        found.append("min_panel_width 比 panel_width 还大")
    if window.min_panel_height > window.panel_height:
        found.append("min_panel_height 比 panel_height 还大")
    if window.edge_fade * 2 >= window.panel_height:
        found.append(f"edge_fade = {window.edge_fade} 太大，上下渐隐加起来超过了面板高度")

    if window.type_hint not in TYPE_HINTS:
        found.append(f"type_hint = {window.type_hint!r} 应该是 {TYPE_HINTS} 之一")
    if window.font_weight not in FONT_WEIGHTS:
        found.append(f"font_weight = {window.font_weight!r} 应该是 {FONT_WEIGHTS} 之一")
    for name in COLOUR_KEYS:
        value = getattr(window, name)
        if not _COLOUR_RE.match(value.strip()):
            found.append(f"{name} = {value!r} 不是一个颜色（#RRGGBB 或 #RRGGBBAA）")
    return found


def _wrong_kind(expected: type, value: object) -> bool:
    """Whether a setting is of a kind it cannot be. An int is fine for a float."""
    if expected is bool:
        return not isinstance(value, bool)
    if expected is int:
        return isinstance(value, bool) or not isinstance(value, int)
    if expected is float:
        return isinstance(value, bool) or not isinstance(value, (int, float))
    return not isinstance(value, expected)


def _kind_name(expected: type) -> str:
    return {int: "整数", float: "数字", bool: "开关（true/false）", str: "文字",
            list: "列表"}.get(expected, "正确的类型")


def seed(path: Path = CONFIG_PATH) -> None:
    """First run: the shipped defaults become the user's settings file."""
    try:
        text = DEFAULTS_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"PlanaLyrics: 读不到默认设置 {DEFAULTS_PATH} ({exc})，使用内置默认值")
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        print(f"PlanaLyrics: 无法创建 {path} ({exc})")


def save_values(changes: dict[str, dict[str, object]], path: Path = CONFIG_PATH) -> None:
    """Write settings back into config.toml without disturbing the file.

    The file is hand-edited and its comments are the only documentation there is,
    so it is never regenerated: each key's value is swapped on the line where it
    already lives, keeping the alignment and the trailing comment. A key the file
    does not mention yet is appended to the end of its section.
    """
    global _self_write
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        print(f"PlanaLyrics: 无法读取 {path} ({exc})")
        return

    pending = {section: dict(values) for section, values in changes.items()}
    insert_at: dict[str, int] = {}
    section = ""
    for index, raw in enumerate(lines):
        stripped = raw.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1].strip()
            continue
        if section and stripped and not stripped.startswith("#"):
            insert_at[section] = index + 1
        if not section or stripped.startswith("#") or "=" not in raw:
            continue
        wanted = pending.get(section)
        key = raw.split("=", 1)[0].strip()
        if wanted and key in wanted:
            lines[index] = _reline(raw, wanted.pop(key))

    # append whatever the file did not have, from the bottom up so the earlier
    # insertion points stay valid
    for section in sorted(pending, key=lambda name: insert_at.get(name, len(lines)), reverse=True):
        leftover = pending[section]
        if not leftover:
            continue
        at = insert_at.get(section, len(lines))
        lines[at:at] = [_documented_line(section, key, value) for key, value in leftover.items()]

    try:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"PlanaLyrics: 无法写入 {path} ({exc})")
        return
    _self_write = time.monotonic()


def restore_defaults(path: Path = CONFIG_PATH) -> bool:
    """Put the shipped defaults back, comments and all.

    The settings file is replaced wholesale, so what was there is kept beside it
    first -- a restore is otherwise one click away from being regretted. The
    defaults file itself is only ever read; nothing the app does writes to it.
    """
    try:
        text = DEFAULTS_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"PlanaLyrics: 读不到默认设置 {DEFAULTS_PATH} ({exc})")
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            backup = path.with_name(path.name + ".bak")
            backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        print(f"PlanaLyrics: 无法恢复默认设置 ({exc})")
        return False
    global _self_write
    _self_write = time.monotonic()
    return True


def request_reset() -> None:
    """Ask a run in progress to put its window back.

    The icon's "restore default settings" action is its own short-lived process:
    it can rewrite the settings file, but not move a window that belongs to the
    run already going. It leaves this note instead, and the run finds it when it
    looks at the file that was just written.
    """
    try:
        RESET_REQUEST.parent.mkdir(parents=True, exist_ok=True)
        RESET_REQUEST.touch()
    except OSError as exc:
        print(f"PlanaLyrics: 无法写入 {RESET_REQUEST} ({exc})")


def take_reset_request() -> bool:
    """True once if a reset was asked for, and the note is then taken away."""
    try:
        RESET_REQUEST.unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:
        print(f"PlanaLyrics: 无法删除 {RESET_REQUEST} ({exc})")
        return False
    return True


def _reline(raw: str, value: object) -> str:
    """``raw`` with its value replaced, everything else on the line kept."""
    head, comment, column = _split_setting(raw)
    left = f"{head.rstrip()} = {_toml(value)}"
    if comment is None:
        return left
    return left + " " * max(1, column - len(left)) + comment


def _documented_line(section: str, key: str, value: object) -> str:
    """The line to append for a setting the settings file does not mention yet.

    Taken from the defaults file when it has the setting, so a new one arrives
    with the same comment that explains it there rather than bare.
    """
    try:
        text = DEFAULTS_PATH.read_text(encoding="utf-8")
    except OSError:
        return f"{key} = {_toml(value)}"
    current = ""
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            current = stripped[1:-1].strip()
        elif current == section and stripped.split("=")[0].strip() == key:
            return _reline(raw, value)
    return f"{key} = {_toml(value)}"


def _split_setting(raw: str) -> tuple[str, str | None, int]:
    """``(the text before the value, the trailing comment, its column)``."""
    head, separator, tail = raw.partition("=")
    if not separator:
        return raw, None, 0
    quote = ""
    for index, char in enumerate(tail):
        if quote:
            quote = "" if char == quote else quote
        elif char in "\"'":
            quote = char
        elif char == "#":
            return head, tail[index:], len(head) + 1 + index
    return head, None, 0


def _toml(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    return repr(value) if isinstance(value, float) else str(value)


def watch(on_change: Callable[[], None], path: Path = CONFIG_PATH) -> Gio.FileMonitor:
    monitor = Gio.File.new_for_path(str(path)).monitor_file(Gio.FileMonitorFlags.NONE, None)

    def _changed(*_args) -> None:
        # a setting the app just wrote is already in effect; reacting to it would
        # re-resolve the lyrics and undo the work a moment after doing it
        if time.monotonic() - _self_write < 1.0:
            return
        on_change()

    monitor.connect("changed", _changed)
    return monitor


def _apply(target: object, values: dict | None) -> None:
    if not values:
        return
    for key, value in values.items():
        if hasattr(target, key) and not key.startswith("_"):
            setattr(target, key, value)
