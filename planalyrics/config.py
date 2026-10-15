"""Configuration and persisted window state.

config.default.toml, shipped beside the source, holds the defaults: it seeds
~/.config/PlanaLyrics/config.toml on first run, and the menu's "restore defaults"
copies it back over whatever is there.

config.toml stays hand-editable. The app writes to it too -- a setting changed
from the right-click menu has to survive a restart -- but it is edited a line at
a time rather than regenerated, so the comments explaining every setting stay
where they are. What the app remembers about the window itself (where it was
dragged to, how big it was dragged) lives in state.json instead: that is
position, not a setting.
"""

from __future__ import annotations

import json
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from gi.repository import Gio

CONFIG_DIR = Path.home() / ".config" / "PlanaLyrics"
CONFIG_PATH = CONFIG_DIR / "config.toml"
STATE_PATH = CONFIG_DIR / "state.json"
CACHE_DIR = Path.home() / ".cache" / "PlanaLyrics"
DEFAULTS_PATH = Path(__file__).resolve().parent.parent / "config.default.toml"

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
    font_family: str = "Noto Sans CJK SC"
    font_weight: str = "black"
    font_lyrics: int = 34
    font_translation: int = 23
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
    # what config.toml itself declares for the panel size, before any remembered
    # size is applied -- needed to tell "default edited" from "user dragged"
    declared_panel: tuple[int, int] = (0, 0)


def load(path: Path = CONFIG_PATH, state_path: Path = STATE_PATH) -> Config:
    if not path.exists():
        seed(path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        print(f"PlanaLyrics: 无法读取 {path} ({exc})，使用默认配置")
        data = {}

    config = Config()
    _apply(config.player, data.get("player"))
    _apply(config.window, data.get("window"))
    _apply(config.lyrics, data.get("lyrics"))
    config.declared_panel = (int(config.window.panel_width), int(config.window.panel_height))

    state = load_state(state_path)
    if state.get("x") is not None:
        config.window.x = int(state["x"])
    if state.get("y") is not None:
        config.window.y = int(state["y"])

    # A dragged panel size is remembered, but editing the default in config.toml
    # wins over it -- otherwise a changed default would appear to do nothing.
    saved = state.get("panel")
    if saved and list(state.get("default_panel") or []) == list(config.declared_panel):
        config.window.panel_width = int(saved[0])
        config.window.panel_height = int(saved[1])
    return config


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
        lines[at:at] = [f"{key} = {_toml(value)}" for key, value in leftover.items()]

    try:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"PlanaLyrics: 无法写入 {path} ({exc})")
        return
    _self_write = time.monotonic()


def restore_defaults(path: Path = CONFIG_PATH, state_path: Path = STATE_PATH) -> bool:
    """Put the shipped defaults back, comments and all.

    The settings file is replaced wholesale, so what was there is kept beside it
    first -- a restore is otherwise one click away from being regretted. What the
    app remembered about the window goes too, or the restored file would appear
    not to have applied.
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
        state_path.unlink(missing_ok=True)
    except OSError as exc:
        print(f"PlanaLyrics: 无法恢复默认设置 ({exc})")
        return False
    global _self_write
    _self_write = time.monotonic()
    return True


def _reline(raw: str, value: object) -> str:
    """``raw`` with its value replaced, everything else on the line kept."""
    head, comment, column = _split_setting(raw)
    left = f"{head.rstrip()} = {_toml(value)}"
    if comment is None:
        return left
    return left + " " * max(1, column - len(left)) + comment


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


def load_state(path: Path = STATE_PATH) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(
    config: Config,
    panel: tuple[int, int] | None = None,
    path: Path = STATE_PATH,
) -> None:
    payload = {
        "x": int(config.window.x),
        "y": int(config.window.y),
        "default_panel": [int(v) for v in (config.declared_panel or (
            config.window.panel_width, config.window.panel_height
        ))],
    }
    if panel is not None:
        payload["panel"] = [int(panel[0]), int(panel[1])]
    else:
        previous = load_state(path).get("panel")
        if previous:
            payload["panel"] = [int(previous[0]), int(previous[1])]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError as exc:
        print(f"PlanaLyrics: 无法保存 {path} ({exc})")


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
