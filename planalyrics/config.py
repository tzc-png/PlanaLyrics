"""Configuration and persisted window state.

config.toml is hand-edited by the user, so it is never rewritten -- comments
would be lost. Things the app needs to remember (window position, lyrics
offset) live in state.json instead.
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from gi.repository import Gio

CONFIG_DIR = Path.home() / ".config" / "PlanaLyrics"
CONFIG_PATH = CONFIG_DIR / "config.toml"
STATE_PATH = CONFIG_DIR / "state.json"
CACHE_DIR = Path.home() / ".cache" / "PlanaLyrics"

EXAMPLE_TOML = """\
# PlanaLyrics -- 歌词覆盖层配置
# 改完保存即自动生效，不需要重启。
#
# 颜色格式为 #RRGGBBAA，最后两位是不透明度：FF=完全不透明，00=全透明。

[player]
# 留空 = 自动跟随正在播放的来源：MyUne Music、浏览器（B站等）、LX Music 都支持
# 想固定某一个就填它的名字，例如 "org.mpris.MediaPlayer2.myune_music" 或 "lxmusic"
pin = ""
lxmusic_port = 23330       # LX Music 的“开放 API”端口（需在 LX Music 设置里打开）

[window]
x = 200                    # 窗口位置；用鼠标拖动后会自动写回 state.json
y = 900
type_hint = "normal"        # normal / utility / dock / notification
show_in_taskbar = true      # true = 出现在左侧坞栏里（也能从应用程序列表启动）

# 面板：固定尺寸，中间透明，只有最外层一圈描边
# 下面的宽高是“默认大小”，拖动窗口边缘调整后会把实际大小记到 state.json。
panel_width = 578
panel_height = 287
min_panel_width = 320      # 拖动缩放时的最小尺寸
min_panel_height = 120
corner_radius = 14
border_width = 2
border_color = "#FFFFFF8C"
glow_size = 10             # 描边外侧的柔和光晕宽度（像素），0 = 关闭
glow_color = "#00000059"   # 深色=外阴影（浅色壁纸下边界更清楚）；白色则像发光

# 歌词排版（字号会随面板缩放自动等比变化）
anchor_ratio = 0.5         # 当前句在面板里的垂直位置：0=顶部 1=底部
context_lines = 1          # 当前句前后各显示几句
context_opacity = 0.5     # 前后那几句的不透明度
context_scale = 0.75       # 前后那几句的字号比例（切句时会平滑放大/缩小）
blur_px = 5                # 前后那几句的模糊半径（像素），0 = 不模糊
font_family = "Noto Sans CJK SC"
font_weight = "black"      # thin/light/normal/medium/semibold/bold/heavy/black
font_lyrics = 34           # 当前句字号（前后句按 context_scale 缩小）
font_translation = 23      # 译文行
line_spacing = 24          # 相邻两句之间的间距
inner_spacing = 3          # 一句歌词与它自己的译文之间的间距
padding = 28               # 文字与面板左右边缘的距离
edge_fade = 14             # 面板上下边缘的文字渐隐高度

# 文字：深色壁纸下用这套
color_current = "#FFFFFFFF"    # 当前句
color_lyrics = "#FFFFFFFF"     # 其他句（再乘以上面的不透明度）
color_translation = "#FFFFFFA6"
halo_radius = 0                # 0 = 纯色字，不加描边
halo_color = "#000000"         # halo_radius > 0 时才生效
halo_alpha = 0.9

# 文字：浅色壁纸下用这套（模仿 ref：近黑字 + 中灰译文 + 白色描边）
adapt_to_background = true     # 按面板背后壁纸的明暗自动切换以上两套
light_color_current = "#0A0A0AFF"
light_color_lyrics = "#0A0A0AFF"
light_color_translation = "#5A6270FF"
light_halo_color = "#FFFFFF"

animation_ms = 500         # 滚动到下一句所需时间；0 = 直接跳转
show_translation = true    # 仅当原文是日语时才显示译文
click_through = false      # true = 鼠标穿透窗口（不能拖动）

[lyrics]
music_dirs = []            # 本地音乐目录，用于在播放器不给文件路径时反查
cache_dir = ""
online_enabled = true      # 本地找不到时就联网搜索
offset_ms = 0              # 歌词整体微调，负数=提前
"""


@dataclass
class PlayerConfig:
    pin: str = ""
    lxmusic_port: int = 23330
    poll_ms: int = 500


@dataclass
class WindowConfig:
    x: int = 200
    y: int = 900
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
    context_opacity: float = 0.5
    context_scale: float = 0.75
    blur_px: int = 5
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
    halo_alpha: float = 0.9
    halo_radius: int = 2
    animation_ms: int = 500
    show_translation: bool = True
    click_through: bool = False


@dataclass
class LyricsConfig:
    music_dirs: list[str] = field(default_factory=list)
    cache_dir: str = ""
    online_enabled: bool = True
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
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(EXAMPLE_TOML, encoding="utf-8")
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
    if state.get("offset_ms") is not None:
        config.lyrics.offset_ms = int(state["offset_ms"])

    # A dragged panel size is remembered, but editing the default in config.toml
    # wins over it -- otherwise a changed default would appear to do nothing.
    saved = state.get("panel")
    if saved and list(state.get("default_panel") or []) == list(config.declared_panel):
        config.window.panel_width = int(saved[0])
        config.window.panel_height = int(saved[1])
    return config


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
        "offset_ms": int(config.lyrics.offset_ms),
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
    monitor.connect("changed", lambda *_: on_change())
    return monitor


def _apply(target: object, values: dict | None) -> None:
    if not values:
        return
    for key, value in values.items():
        if hasattr(target, key) and not key.startswith("_"):
            setattr(target, key, value)
