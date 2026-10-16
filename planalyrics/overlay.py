"""The fixed-size transparent lyrics panel.

Layout follows BetterLyrics' desktop mode: one font size for every line, the
current line pinned at a fixed height, one line of context on each side. Faded
lines are genuinely blurred (BetterLyrics uses a Win2D Gaussian; this does a
separable box blur on the pixels, which converges on the same thing).

Text colour adapts to the wallpaper behind the panel, so the light-theme scheme
can match the reference (near-black text) without becoming invisible when the
desktop is dark.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, replace

import cairo
import gi
import numpy as np

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")

from gi.repository import Gdk, GLib, Gtk, Pango, PangoCairo  # noqa: E402

from .config import Config  # noqa: E402
from .lyrics import Lyrics  # noqa: E402

HINTS = {
    "normal": Gdk.WindowTypeHint.NORMAL,
    "utility": Gdk.WindowTypeHint.UTILITY,
    "dock": Gdk.WindowTypeHint.DOCK,
    "notification": Gdk.WindowTypeHint.NOTIFICATION,
}
HALO_DIRECTIONS = ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (1, -1), (-1, 1), (1, 1))
CURSORS = {
    "n": "ns-resize", "s": "ns-resize", "e": "ew-resize", "w": "ew-resize",
    "ne": "nesw-resize", "sw": "nesw-resize", "nw": "nwse-resize", "se": "nwse-resize",
}
CURRENT, TRANSLATION, LYRICS = "current", "translation", "lyrics"
FONT_WEIGHTS = {
    "thin": Pango.Weight.THIN,
    "light": Pango.Weight.LIGHT,
    "normal": Pango.Weight.NORMAL,
    "medium": Pango.Weight.MEDIUM,
    "semibold": Pango.Weight.SEMIBOLD,
    "bold": Pango.Weight.BOLD,
    "heavy": Pango.Weight.HEAVY,
    "black": Pango.Weight.ULTRAHEAVY,
}
DRAG_THRESHOLD = 3
RESIZE_ZONE = 9
ANIMATION_FRAME_MS = 16
INVISIBLE = 0.02
HALO_FADE_FLOOR = 0.55
TEXT_CACHE_LIMIT = 32
# the downscale is itself part of the blur, and at 1/4 it contributed more of the
# softening than the box passes did: the neighbours came out as a smudge whose
# strokes were gone, with a line and its translation merged into one dark mass.
# At 1/2 the blur is still real but the text stays readable behind it.
BLUR_DOWNSCALE = 2
BLUR_PASSES = 3           # must not change between sliding and still: a weaker
                          # blur mid-slide reads as the lines getting bolder
BACKGROUND_SAMPLE_MS = 500
# the light/dark text colour fades between the two schemes rather than flipping
THEME_FADE_MS = 700
THEME_FRAME_MS = 16
# Karaoke wipe: the current line lights up left to right over its own span. How
# much of the interval between two lines is swept, and the cap that keeps a long
# instrumental break from crawling across the line, are both configurable.
KARAOKE_DEFAULT_MS = 4000.0
KARAOKE_FRAME_MS = 16
# Only redraw once the edge has moved this far along the line -- about a fifth of
# a pixel on a full-width one, so the sweep is smooth without a frame per tick.
WIPE_EPSILON = 0.0015
# the lyric size karaoke_lift's pixels were tuned at: the lift scales with the
# font so it keeps the same share of the glyph height as the font changes
KARAOKE_LIFT_REFERENCE = 34.0
# The little light in the bottom-left corner. It is there because "still working"
# and "done" are otherwise invisible on a window with no other chrome: a lookup
# takes seconds and leaves nothing behind to say it had been out. Red while one is
# in flight, green once it has been answered -- answered with nothing included,
# since a song no site has a lyric for is not work still outstanding.
STATUS_LIGHT_RADIUS = 4.5
STATUS_LIGHT_INSET = 12.0
STATUS_BUSY_RGB = (0.94, 0.29, 0.26)
STATUS_IDLE_RGB = (0.26, 0.80, 0.35)
# Word-level timings a lyric can carry, best first. Each entry is the tail of its
# setting -- ``karaoke_awlrc`` and so on -- the label the menu shows, and the keys
# its timing is stored under: one apiece for the lyric's own sources, one per site
# for the online chain, whose sites are asked in turn. The menu lists them in this
# order and the sweep tries them in it -- the first one switched on that covers the
# line being sung wins -- and the ramp over the line's average pace is what is left
# when none of them can.
#
# Every timing the chain can attach belongs in one of these tuples. A source left
# out is not merely unlisted: its timing is fetched, put on the lines, and then
# never looked at, so the sweep ramps over the average pace and both the menu and
# the log report the song as having no word timing at all. The keys inside a group
# are kept in the order sources.py asks them in, so the two lists cannot drift
# apart.
TIMELINE_SOURCES = (
    ("awlrc", "使用awlrc", ("awlrc",)),
    ("online", "使用联网逐字歌词", ("krc", "qrc", "yrc")),
    ("enhanced", "支持增强LRC", ("enhanced",)),
)


@dataclass
class VisualRow:
    """A line of text as it should look right now."""

    key: str
    text: str
    role: str
    size: int
    y: float
    alpha: float
    attention: bool = False
    # 1 = drawn crisp, 0 = drawn through the blur. Animated during a transition so
    # a line sharpens as it arrives and softens as it leaves, instead of flipping.
    sharp: float = 0.0
    # NB: no colour here. The colour depends only on the role and the current
    # light/dark theme, and resolving it at draw time is what lets the theme fade
    # (and keeps a theme change from disturbing a line animation in flight).


@dataclass
class _Block:
    index: int
    primary: Pango.Layout
    translation: Pango.Layout | None
    height: float
    size: int
    translation_size: int
    # what the line will really be drawn at, which is not what laying it out at
    # this size reports -- see _drawn_height
    primary_height: float = 0.0
    top: float = 0.0


class Overlay:
    """Draws itself on a child DrawingArea.

    Everything is painted in the area's ``draw`` handler, which returns True so
    GtkDrawingArea's default handler -- and with it any theme background -- never
    runs. The window is a fixed rectangle the user can drag or resize by its
    edges; the lyrics scale with it rather than reflowing.
    """

    def __init__(self, config: Config, app) -> None:
        self.app = app
        self.config = config
        self.lyrics: Lyrics | None = None
        self.index: int | None = None
        self.status = ""
        self.busy = False
        self.menu: Gtk.Menu | None = None
        # 0 = light text (dark background), 1 = dark text (light background)
        self._theme_mix = 0.0
        self._theme_target = 0.0
        self._theme_from = 0.0
        self._theme_start = 0.0
        self._theme_duration = 0.0
        self._theme_ready = False
        self._theme_timer = 0
        self._panel = (int(config.window.panel_width), int(config.window.panel_height))
        self._size: tuple[int, int] | None = None
        self._drag: tuple | None = None
        self._dragged = False
        self._resize: tuple | None = None
        self._cursor_name: str | None = None
        self._cursors: dict[str, Gdk.Cursor] = {}
        self._blur_buffer = None
        self._blur_surface = None
        self._blur_key: tuple | None = None
        self._frame: cairo.ImageSurface | None = None
        self._text_cache: dict[tuple, tuple] = {}
        self._font_options = cairo.FontOptions()
        self._font_options.set_antialias(cairo.Antialias.GRAY)
        self._from_rows: list[VisualRow] | None = None
        self._to_rows: list[VisualRow] = []
        self._progress = 1.0
        self._anim_start = 0.0
        self._anim_duration = 0.0
        self._anim_timer = 0
        # playback clock: the app hands over an interpolated position every tick,
        # and the karaoke sweep reads it forward between ticks so the wipe moves
        # at frame rate instead of stepping in 100 ms jumps
        self._clock_ms = 0.0
        self._clock_at = time.monotonic()
        self._clock_playing = False
        self._karaoke_timer = 0
        self._wipe_drawn = -1.0
        self._build_window()

    # -- window setup ----------------------------------------------------

    def _build_window(self) -> None:
        window = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        window.set_name("planalyrics")
        # WM_CLASS has to match the desktop entry's StartupWMClass, otherwise the
        # dock cannot associate the running window with the launcher
        window.set_wmclass("planalyrics", "planalyrics")
        window.set_app_paintable(True)
        window.set_decorated(False)
        window.set_resizable(False)
        window.set_skip_taskbar_hint(not self.config.window.show_in_taskbar)
        window.set_skip_pager_hint(not self.config.window.show_in_taskbar)
        window.set_accept_focus(False)
        window.set_focus_on_map(False)
        window.set_type_hint(HINTS.get(self.config.window.type_hint, Gdk.WindowTypeHint.UTILITY))
        window.set_keep_above(True)
        # on every workspace when asked, not only the one it happened to open on:
        # the lyrics belong to whatever is playing, and changing pages should not
        # hide them
        if self.config.window.all_workspaces:
            window.stick()
        self._apply_rgba_visual(window)

        area = Gtk.DrawingArea()
        area.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
            | Gdk.EventMask.LEAVE_NOTIFY_MASK
        )
        area.connect("draw", self._on_draw)
        area.connect("button-press-event", self._on_button_press)
        area.connect("button-release-event", self._on_button_release)
        area.connect("motion-notify-event", self._on_motion)
        area.connect("leave-notify-event", self._on_leave)
        window.add(area)

        window.connect("screen-changed", self._on_screen_changed)
        window.connect("window-state-event", self._on_window_state)
        window.connect("delete-event", lambda *_: (self.app.quit(), True)[1])
        window.move(self.config.window.x, self.config.window.y)
        window.show_all()

        self.window = window
        self.area = area
        self._build_menu()
        self._apply_panel_size()
        GLib.timeout_add(400, self._sample_background)
        GLib.timeout_add(BACKGROUND_SAMPLE_MS, self._on_sample_tick)

    def _apply_rgba_visual(self, window: Gtk.Window) -> None:
        screen = window.get_screen()
        if screen is not None and screen.is_composited():
            visual = screen.get_rgba_visual()
            if visual is not None:
                window.set_visual(visual)

    def _on_screen_changed(self, window, _previous) -> bool:
        self._apply_rgba_visual(window)
        return False

    def _on_window_state(self, _window, event) -> bool:
        wanted = Gdk.WindowState.ABOVE
        if self.config.window.all_workspaces:
            wanted |= Gdk.WindowState.STICKY
        if event.changed_mask & wanted and (event.new_window_state & wanted) != wanted:
            GLib.idle_add(self._apply_window_states)
        return False

    def _apply_window_states(self) -> bool:
        """The states a panel keeps: above other windows, and on every workspace.

        A window manager may clear either when focus moves between full-screen
        windows, so both are reapplied whenever one is seen to go -- which is what
        this returns to, as well as what the menu's toggle calls.
        """
        self.window.set_keep_above(True)
        if self.config.window.all_workspaces:
            self.window.stick()
        else:
            self.window.unstick()
        return False

    def _margin(self) -> int:
        return max(0, int(self.config.window.glow_size))

    def _apply_panel_size(self) -> None:
        margin = self._margin()
        size = (int(self._panel[0]) + 2 * margin, int(self._panel[1]) + 2 * margin)
        if size != self._size:
            self._size = size
            self._text_cache.clear()      # the wrap width changed
            self._frame = None            # and the layer the border is drawn into
            self.area.set_size_request(*size)

    # -- public API ------------------------------------------------------

    def set_lyrics(self, lyrics: Lyrics) -> None:
        """Show a lyric, keeping the line on screen where it is still the same one.

        A song can hand its lyric over twice: what the file holds, and then the
        same lyric with the word timing a site has added to it. Those are the same
        lines and the song has not moved, so the view is left alone -- clearing
        the index would take the display off the line being sung and back onto it
        to say that nothing about it had changed.
        """
        before = (
            [(line.t_ms, line.text) for line in self.lyrics.lines]
            if self.lyrics is not None
            else None
        )
        self.lyrics = lyrics
        if before != [(line.t_ms, line.text) for line in lyrics.lines]:
            self.index = None
        self.status = ""
        self._snap()

    def set_status(self, text: str) -> None:
        self.lyrics = None
        self.index = None
        self.status = text
        self._snap()

    def set_busy(self, busy: bool) -> None:
        """Say whether a lookup is still out, which the corner light shows."""
        if busy != self.busy:
            self.busy = busy
            self.area.queue_draw()

    def set_index(self, index: int) -> None:
        if index != self.index:
            previous = self._draw_rows()
            self.index = index
            self._wipe_drawn = -1.0
            self._animate_from(previous, index)
            self._sync_karaoke_timer()

    def set_position(self, t_ms: float, playing: bool) -> None:
        """Adopt the app's playback position as an anchor for the sweep.

        The app already interpolates between the player's position reports; this
        just lets the overlay keep reading that clock forward at frame rate, so
        the wipe does not advance in 100 ms steps.
        """
        was = self._clock_ms
        self._clock_ms = float(t_ms)
        self._clock_at = time.monotonic()
        self._clock_playing = bool(playing)
        self._sync_karaoke_timer()
        if not playing and abs(float(t_ms) - was) > 1.0:
            # a paused seek still needs one repaint to catch the sweep up
            self.area.queue_draw()

    def force_redraw(self) -> None:
        self._snap()

    def panel_size(self) -> tuple[int, int]:
        return self._panel

    def window_position(self) -> tuple[int, int]:
        """Where the window sits now, to be saved as the place it opens at."""
        return self.window.get_position()

    def apply_config(self, config: Config, move_window: bool = False,
                     force_panel: bool = False) -> None:
        """Adopt a settings file.

        ``force_panel`` takes the file's panel size even when it has not changed,
        which is what "reload config" means: the size on screen may have been
        dragged away from what the file says. ``move_window`` puts the window
        back where the file says it belongs; "reload config" and "restore
        defaults" both ask for that, since a position in the file is meant to be
        where the window sits.
        """
        declared = (int(config.window.panel_width), int(config.window.panel_height))
        if force_panel or declared != (int(self.config.window.panel_width),
                                       int(self.config.window.panel_height)):
            self._panel = declared
        self.config = config
        self._text_cache.clear()
        self._blur_key = None             # colours and blur size may have changed
        self._frame = None                # so may the border and its glow
        self.window.set_type_hint(HINTS.get(config.window.type_hint, Gdk.WindowTypeHint.UTILITY))
        self._apply_window_states()
        self._build_menu()
        self._apply_panel_size()
        if move_window:
            # deferred: the move must reach the window manager after the new size
            # does -- see _move_window
            GLib.idle_add(self._move_window, int(config.window.x), int(config.window.y))
        else:
            self._sample_background()
        self._snap()

    def _move_window(self, x: int, y: int) -> bool:
        """Put the window where the settings file says, once the new size is in.

        A move sent while the window still has its old size is clamped by the
        window manager whenever that size would not fit where it is being sent:
        a large panel sent to a bottom corner is pushed back on screen, and the
        resize that follows does not put it back where it was asked to go. That
        left the first "reload config" landing in the wrong place, and only a
        second one -- with nothing left to resize -- getting it right.
        set_size_request applies on a GTK idle, so this waits for one, which runs
        after the resize and sends the move with the new size already in place.
        """
        self.window.move(x, y)
        self._sample_background()
        return False

    # -- animation -------------------------------------------------------

    def _snap(self) -> None:
        self._cancel_animation()
        self._from_rows = None
        self._to_rows = []
        self._progress = 1.0
        self._sync_karaoke_timer()
        self.area.queue_draw()

    def _cancel_animation(self) -> None:
        if self._anim_timer:
            GLib.source_remove(self._anim_timer)
            self._anim_timer = 0

    def _animate_from(self, previous: list[VisualRow], index: int) -> None:
        duration_ms = max(0, int(self.config.window.animation_ms))
        if duration_ms == 0 or not previous:
            self._snap()
            return

        self._from_rows = previous
        self._to_rows = self._settled_rows(index)
        self._progress = 0.0
        self._anim_start = time.monotonic()
        self._anim_duration = duration_ms / 1000.0
        if not self._anim_timer:
            self._anim_timer = GLib.timeout_add(ANIMATION_FRAME_MS, self._animate_frame)

    def _animate_frame(self) -> bool:
        elapsed = time.monotonic() - self._anim_start
        raw = 1.0 if not self._anim_duration else elapsed / self._anim_duration
        self._progress = _ease(min(1.0, raw))
        self.area.queue_draw()
        if raw >= 1.0:
            self._cancel_animation()
            self._from_rows = None
            self._to_rows = []
            self._progress = 1.0
            self.area.queue_draw()
            return False
        return True

    # -- karaoke sweep ---------------------------------------------------

    def _sync_karaoke_timer(self) -> None:
        """Run the sweep clock only while there is something to sweep.

        Paused -- or with no synced lyrics to time a sweep against -- the timer
        stops, so an idle panel does no work at all.
        """
        want = self._clock_playing and self._wipe_span() is not None
        if want and not self._karaoke_timer:
            self._karaoke_timer = GLib.timeout_add(KARAOKE_FRAME_MS, self._karaoke_frame)
        elif not want and self._karaoke_timer:
            GLib.source_remove(self._karaoke_timer)
            self._karaoke_timer = 0

    def _karaoke_frame(self) -> bool:
        progress = self._wipe_now()
        if progress is not None and abs(progress - self._wipe_drawn) > WIPE_EPSILON:
            self._wipe_drawn = progress
            self.area.queue_draw()
        return True

    def _clock_now(self) -> float:
        """Playback time in ms, read forward from the last anchor."""
        if not self._clock_playing:
            return self._clock_ms
        return self._clock_ms + (time.monotonic() - self._clock_at) * 1000.0

    def _wipe_span(self) -> tuple[float, float] | None:
        """``(start_ms, sweep_ms)`` for the line being sung, or None.

        A line owns the whole interval up to the next timestamp, but the voice
        generally stops before then, so only part of it is swept; long intervals
        (an instrumental break) are capped rather than crawled.
        """
        window, lyrics, index = self.config.window, self.lyrics, self.index
        if not window.karaoke or lyrics is None or not lyrics.synced:
            return None
        if index is None or not (0 <= index < len(lyrics.lines)):
            return None
        start = float(lyrics.lines[index].t_ms)
        if index + 1 < len(lyrics.lines):
            span = float(lyrics.lines[index + 1].t_ms) - start
        else:
            span = KARAOKE_DEFAULT_MS
        if span <= 0:
            span = KARAOKE_DEFAULT_MS
        tail = max(0.05, min(1.0, float(window.karaoke_tail)))
        cap = max(200.0, float(window.karaoke_max_ms))
        return start, max(150.0, min(span * tail, cap))

    def _wipe_now(self) -> float | None:
        span = self._wipe_span()
        if span is None:
            return None
        start, sweep = span
        now = self._clock_now()
        exact = self._exact_progress(now - start)
        if exact is not None:
            return exact
        return max(0.0, min(1.0, (now - start) / sweep))

    def _exact_progress(self, elapsed_ms: float) -> float | None:
        """The sweep from the line's own timing, if a switched-on source has one.

        The sources are tried in the order the menu lists them, so what the
        settings say about precedence is literally what happens. None when none
        of them can place the line, leaving the caller the average pace.
        """
        line = self._current_line()
        if line is None:
            return None
        window = self.config.window
        for name, _label, keys in TIMELINE_SOURCES:
            if not getattr(window, "karaoke_" + name):
                continue
            for key in keys:
                progress = _timeline_progress(line.timelines.get(key), elapsed_ms)
                if progress is not None:
                    return progress
        return None

    def _current_line(self):
        """The line being sung, or None when there is no song to be on."""
        lyrics, index = self.lyrics, self.index
        if lyrics is None or index is None or not (0 <= index < len(lyrics.lines)):
            return None
        return lyrics.lines[index]

    def _active_timeline_source(self) -> str | None:
        """Which timing source is driving the sweep for the line on screen now.

        This is what the menu marks, so the settings say which source is really
        in use and not only which are allowed: one can be switched on and still
        have nothing for this song -- or for this line of it, since the credits
        and the instrumental passages of a lyric usually carry no timing at all.
        None means none of them can place this line, and the average pace is what
        is left.
        """
        line = self._current_line()
        if line is None or not self.config.window.karaoke:
            return None
        for name, _label, keys in TIMELINE_SOURCES:
            if not getattr(self.config.window, "karaoke_" + name):
                continue
            if any(line.timelines.get(key) for key in keys):
                return name
        return None

    def _row_wipe(self, row: VisualRow) -> tuple[float, bool] | None:
        """How much of ``row`` has been sung: ``(progress, sweeps)``, else None.

        Only the line being sung has a sweep. Its translation does not: the
        translation is not what is being sung, so it simply stays dim until the
        line is through. Every other row -- the neighbours, the waiting message --
        is left alone.
        """
        prefix, _, index = row.key.partition("\x00")
        if prefix not in ("line", "translation") or index != str(self.index):
            return None
        progress = self._wipe_now()
        if progress is None:
            return None
        return progress, prefix == "line"

    # -- background sampling ---------------------------------------------

    def _on_sample_tick(self) -> bool:
        # Grabbing the root window makes the compositor produce pixels and costs
        # ~16 ms, so never do it while the lyrics are sliding -- retry next tick.
        if self._from_rows is not None:
            return True
        self._sample_background()
        return True

    def _sample_background(self) -> bool:
        """Decide light or dark text from what the words are actually over.

        That is the panel itself, so the panel is what gets sampled. A strip
        outside it only guesses at what the panel covers: it is wrong wherever
        the panel is against a screen edge, and wrong again wherever what is
        beside the panel is not what is under it.

        The median, not the mean, because our own lyrics are in the same region.
        They cover a fifth of the panel at most, so the middle value is the
        desktop behind them, and the reading does not swing about with how much
        text happens to be on screen -- or with whether the clever choice of
        colour is the one that is currently drawn.
        """
        settings = self.config.window
        if not settings.adapt_to_background:
            self._set_theme(True)
            return False
        root = Gdk.get_default_root_window()
        if self.window.get_window() is None or root is None or self._size is None:
            return False

        x, y = self.window.get_position()
        # the surface carries the glow margin, so the panel starts that far in
        x, y = x + self._margin(), y + self._margin()
        width, height = self._panel
        try:
            pixbuf = Gdk.pixbuf_get_from_window(root, x, y, width, height)
        except Exception:
            pixbuf = None
        if pixbuf is None:
            return False
        self._set_theme(_median_luma(pixbuf) < 128.0)
        return False

    # -- input -----------------------------------------------------------

    def _hit_test(self, x: float, y: float) -> str | None:
        if self._size is None:
            return None
        width, height = self._size
        vertical = "w" if x < RESIZE_ZONE else "e" if x > width - RESIZE_ZONE else ""
        horizontal = "n" if y < RESIZE_ZONE else "s" if y > height - RESIZE_ZONE else ""
        return (horizontal + vertical) or None

    def _set_cursor(self, edge: str | None) -> None:
        if edge == self._cursor_name:
            return
        self._cursor_name = edge
        gdk_window = self.window.get_window()
        if gdk_window is None:
            return
        if edge is None:
            gdk_window.set_cursor(None)
            return
        name = CURSORS[edge]
        cursor = self._cursors.get(name)
        if cursor is None:
            try:
                cursor = Gdk.Cursor.new_from_name(Gdk.Display.get_default(), name)
            except Exception:
                cursor = None
            self._cursors[name] = cursor
        if cursor is not None:
            gdk_window.set_cursor(cursor)

    def _on_leave(self, *_args) -> bool:
        self._set_cursor(None)
        return False

    def _on_button_press(self, _widget, event) -> bool:
        if event.button == 3:
            # rebuilt here so the player list and the toggles are current: it was
            # otherwise frozen at startup, before any player had been discovered
            self._build_menu()
            self.menu.popup_at_pointer(event)
            return True
        if event.button != 1:
            return False

        edge = self._hit_test(event.x, event.y)
        x, y = self.window.get_position()
        if edge is not None:
            self._resize = (edge, event.x_root, event.y_root, x, y, self._panel)
            return True
        self._drag = (event.x_root, event.y_root, x, y)
        self._dragged = False
        return True

    def _on_motion(self, _widget, event) -> bool:
        if not (event.state & Gdk.ModifierType.BUTTON1_MASK):
            # no button held: the pointer is only choosing a resize cursor, and
            # any gesture left over from a missed release must be dropped
            self._resize = None
            self._drag = None
            self._set_cursor(self._hit_test(event.x, event.y))
            return False

        if self._resize is not None:
            self._resize_to(event)
            return True
        if self._drag is not None:
            start_x, start_y, win_x, win_y = self._drag
            delta_x = event.x_root - start_x
            delta_y = event.y_root - start_y
            if not self._dragged and abs(delta_x) < DRAG_THRESHOLD and abs(delta_y) < DRAG_THRESHOLD:
                return True
            self._dragged = True
            self.window.move(int(win_x + delta_x), int(win_y + delta_y))
            return True
        return False

    def _resize_to(self, event) -> None:
        edge, start_x, start_y, win_x, win_y, panel = self._resize
        delta_x = event.x_root - start_x
        delta_y = event.y_root - start_y

        width, height = panel
        x, y = win_x, win_y
        if "e" in edge:
            width += delta_x
        if "w" in edge:
            width -= delta_x
            x += delta_x
        if "s" in edge:
            height += delta_y
        if "n" in edge:
            height -= delta_y
            y += delta_y

        settings = self.config.window
        width = max(int(settings.min_panel_width), int(width))
        height = max(int(settings.min_panel_height), int(height))
        if width == self._panel[0] and height == self._panel[1] and (x, y) == self.window.get_position():
            return

        self.window.move(int(x), int(y))
        self._panel = (width, height)
        self._apply_panel_size()
        self.area.queue_draw()

    def _on_button_release(self, _widget, event) -> bool:
        if event.button != 1:
            return False
        if self._resize is not None:
            self._resize = None
            self._sample_background()
            return True
        if self._drag is not None:
            # where the window was dragged to is this session's business only:
            # nothing is written, so the next run opens it where the settings
            # file says. "Save current window size" in the menu is what makes a
            # size stick.
            self._drag = None
            return True
        return False

    def _build_menu(self) -> None:
        """The right-click menu, grouped by what each entry is about.

        A section label is an entry that is there to be read rather than clicked,
        so each group says what the settings under it have in common. Restoring
        the defaults sits on its own at the end: it is the one entry that cannot
        be undone from this menu.
        """
        menu = Gtk.Menu()

        def heading(text: str) -> None:
            item = Gtk.MenuItem(label=text)
            item.set_sensitive(False)
            menu.append(item)

        def action(label: str, callback):
            item = Gtk.MenuItem(label=label)
            item.connect("activate", lambda *_args: callback())
            menu.append(item)
            return item

        def toggle(label: str, section: str, attribute: str):
            item = Gtk.CheckMenuItem(label=label)
            item.set_active(bool(getattr(getattr(self.config, section), attribute)))
            item.connect("toggled", self._on_toggle, section, attribute)
            menu.append(item)
            return item

        heading("歌词")
        menu.append(self._build_player_menu())
        action("重新搜索歌词", self.app.research)
        toggle("显示翻译（外语歌）", "window", "show_translation")
        toggle("联网搜索歌词", "lyrics", "online_enabled")
        toggle("联网搜索中文译文", "lyrics", "search_translation")

        menu.append(Gtk.SeparatorMenuItem())
        heading("窗口")
        grow = action("增大字号", lambda: self.app.adjust_font(+1))
        grow.set_sensitive(self.app.font_can_grow())
        shrink = action("减小字号", lambda: self.app.adjust_font(-1))
        shrink.set_sensitive(self.app.font_can_shrink())
        toggle("跨页面显示（所有工作区）", "window", "all_workspaces")
        action("保存窗口大小和位置", self.app.save_window_geometry)

        menu.append(Gtk.SeparatorMenuItem())
        heading("显示")
        toggle("逐字进度（唱到哪亮到哪）", "window", "karaoke")
        active = self._active_timeline_source()
        for name, label, _keys in TIMELINE_SOURCES:
            mark = "　（当前）" if name == active else ""
            item = toggle(label + mark, "window", "karaoke_" + name)
            # sub-settings of the sweep above them, indented and inert without it.
            # The margin, not a leading space in the label, is what carries the
            # tick in with it: a space would only shift the words.
            item.set_margin_start(20)
            item.set_sensitive(bool(self.config.window.karaoke))
        toggle("跟随壁纸明暗", "window", "adapt_to_background")

        menu.append(Gtk.SeparatorMenuItem())
        heading("程序")
        action("打开配置文件夹", self.app.open_config_folder)
        action("重载配置", self.app.reload_config)
        action("退出", self.app.quit)

        menu.append(Gtk.SeparatorMenuItem())
        action("恢复默认设置", self.app.restore_defaults)

        menu.show_all()
        self.menu = menu

    def _build_player_menu(self) -> Gtk.MenuItem:
        """Pick which player to follow; the default follows whatever is playing."""
        item = Gtk.MenuItem(label="播放器")
        submenu = Gtk.Menu()
        active = self.app.active_player()
        pinned = self.app.pinned_player()

        group: Gtk.RadioMenuItem | None = None
        for key, label, playing in self.app.player_options():
            marks = []
            if key == active:
                marks.append("当前")
            if playing:
                marks.append("播放中")
            text = label + (f"   ({'，'.join(marks)})" if marks else "")
            entry = Gtk.RadioMenuItem(label=text)
            if group is None:
                group = entry
            else:
                entry.join_group(group)
            entry.set_active(key == pinned)          # a tick means "pinned"
            entry.connect("toggled", self._on_pick_player, key)
            submenu.append(entry)

        if group is not None:
            submenu.append(Gtk.SeparatorMenuItem())
        auto = Gtk.RadioMenuItem(label="自动跟随正在播放")
        if group is not None:
            auto.join_group(group)
        auto.set_active(not pinned)
        auto.connect("toggled", self._on_pick_player, None)
        submenu.append(auto)

        submenu.show_all()
        item.set_submenu(submenu)
        return item

    def _on_pick_player(self, entry: Gtk.RadioMenuItem, key: str | None) -> None:
        if entry.get_active():
            self.app.select_player(key)

    def _on_toggle(self, item: Gtk.CheckMenuItem, section: str, attribute: str) -> None:
        setattr(getattr(self.config, section), attribute, item.get_active())
        self.app.save_setting(section, attribute, item.get_active())
        if attribute == "adapt_to_background":
            self._sample_background()
        if attribute == "all_workspaces":
            # takes effect at once, without waiting for the next file read
            self._apply_window_states()
        if attribute in ("search_translation", "online_enabled"):
            # which lyrics are on screen changes, so they are resolved again
            self.app.reload_lyrics()
        self._snap()

    # -- lyric layout ----------------------------------------------------

    def _font(self, size: float) -> int:
        """The size a line is drawn at: the configured size at the menu's scale.

        Font size no longer scales with the panel -- the window and the text are
        sized independently, the text from the menu. The menu moves font_scale, so
        every size is that proportion of the value in the file. The floor keeps a
        pathological context_scale from collapsing the neighbours to nothing.
        """
        return max(6, int(round(size * self.config.window.font_scale / 100)))

    def _wrap_width(self) -> int:
        return max(1, int(self._panel[0] - 2 * self.config.window.padding))

    def _layout(self, text: str, size: float) -> Pango.Layout:
        settings = self.config.window
        layout = self.area.create_pango_layout(text or " ")
        description = Pango.FontDescription()
        description.set_family(settings.font_family)
        # Pango sizes are in 1/1024 px, so a fractional size keeps the grow/shrink
        # animation smooth instead of stepping whole pixels
        description.set_absolute_size(max(Pango.SCALE, int(round(size * Pango.SCALE))))
        description.set_weight(
            FONT_WEIGHTS.get(str(settings.font_weight).lower(), Pango.Weight.BOLD)
        )
        layout.set_font_description(description)

        # Antialiasing has to be switched off subpixel-wise on the layout's own
        # PangoContext: a widget-created layout carries its own font options and
        # ignores whatever is set on the cairo context. Subpixel filtering is what
        # puts red/green fringes on the glyphs, which the blur then smears.
        PangoCairo.context_set_font_options(layout.get_context(), self._font_options)

        # CENTER so a line that wraps keeps its rows centred instead of ragged-left
        layout.set_alignment(Pango.Alignment.CENTER)
        layout.set_width(self._wrap_width() * Pango.SCALE)
        layout.set_wrap(Pango.WrapMode.WORD_CHAR)
        return layout

    def _drawn_height(self, text: str, size: float, full_size: int) -> float:
        """The height this row will actually be drawn at.

        A row is rasterised once at the size it has as the current line and then
        blitted scaled, so its height is that rasterisation's times the scale --
        which is not what laying the same text out at the smaller size reports.
        A smaller font fits more on a wrapped row, so a line that needs two rows
        at full size can be measured as one row at ``context_scale``; believing
        that measurement is what let a wrapped line's second row land on top of
        its own translation.
        """
        if full_size <= 0:
            return 0.0
        surface, _width, height, _pad, _lines = self._text_surface(text, full_size)
        if surface is None:
            return 0.0
        return height * (size / full_size)

    def _has_translation(self) -> bool:
        settings = self.config.window
        if not settings.show_translation or self.lyrics is None:
            return False
        if not self.lyrics.wants_translation:
            return False
        return any(line.translation for line in self.lyrics.lines)

    def _blocks(self, index: int) -> list[_Block]:
        """The current line plus ``context_lines`` on each side, positioned so
        the current block is pinned at the anchor.

        Context lines are drawn at ``context_scale`` of the current line's size,
        so their blocks are shorter -- that is what leaves room for every line to
        carry its translation inside a panel the size of the reference's. The
        geometry is the same whichever line is current, because whichever line is
        current is the one drawn at full size.
        """
        settings = self.config.window
        lines = self.lyrics.lines
        scale = max(0.1, float(settings.context_scale))
        inner = settings.inner_spacing

        def block_height(size: int, translation_size: int) -> float:
            height = self._drawn_height("国", size, full_primary)
            if self._has_translation():
                height += inner + self._drawn_height("国", translation_size, full_translation)
            return height

        full_primary = self._font(settings.font_lyrics)
        small_primary = self._font(settings.font_lyrics * scale)
        full_translation = self._font(settings.font_translation)
        small_translation = self._font(settings.font_translation * scale)
        current_reserved = block_height(full_primary, full_translation)
        other_reserved = block_height(small_primary, small_translation)

        context = max(0, int(settings.context_lines))
        reference_index = max(0, min(index, len(lines) - 1))
        first = max(0, reference_index - context)
        last = min(len(lines) - 1, reference_index + context)

        blocks = []
        for i in range(first, last + 1):
            current = i == index
            size = full_primary if current else small_primary
            translation_size = full_translation if current else small_translation
            translation = None
            if self._has_translation() and lines[i].translation:
                translation = self._layout(lines[i].translation, translation_size)
            primary = self._layout(lines[i].text, size)
            primary_height = self._drawn_height(lines[i].text, size, full_primary)
            height = primary_height
            if translation is not None:
                height += inner + self._drawn_height(
                    lines[i].translation, translation_size, full_translation
                )
            reserved = current_reserved if current else other_reserved
            blocks.append(
                _Block(
                    i, primary, translation, max(height, reserved), size, translation_size,
                    primary_height,
                )
            )

        pitch = other_reserved + settings.line_spacing
        anchor = self._panel[1] * settings.anchor_ratio
        position = next(i for i, block in enumerate(blocks) if block.index == reference_index)
        blocks[position].top = anchor + (reference_index - index) * pitch - blocks[position].height / 2

        cursor = blocks[position].top + blocks[position].height
        for block in blocks[position + 1 :]:
            cursor += settings.line_spacing
            block.top = cursor
            cursor += block.height

        cursor = blocks[position].top
        for block in reversed(blocks[:position]):
            cursor -= settings.line_spacing + block.height
            block.top = cursor
        return blocks

    def _settled_rows(self, index: int) -> list[VisualRow]:
        settings = self.config.window
        if self.lyrics is None or not self.lyrics.lines:
            return self._status_rows()

        if not self.lyrics.synced:
            return self._unsynced_rows()

        lines = self.lyrics.lines
        rows: list[VisualRow] = []
        for block in self._blocks(index):
            distance = abs(block.index - index)
            current = distance == 0
            alpha = 1.0 if current else float(settings.context_opacity)
            rows.append(
                VisualRow(
                    f"line\x00{block.index}",
                    lines[block.index].text,
                    CURRENT if current else LYRICS,
                    block.size,
                    block.top,
                    alpha,
                    attention=current,
                    sharp=1.0 if current else 0.0,
                )
            )
            if block.translation is not None:
                rows.append(
                    VisualRow(
                        f"translation\x00{block.index}",
                        lines[block.index].translation,
                        TRANSLATION,
                        block.translation_size,
                        block.top + block.primary_height + settings.inner_spacing,
                        alpha,
                        attention=current,
                        sharp=1.0 if current else 0.0,
                    )
                )
        return rows

    def _color(self, role: str):
        """The role's colour, faded between the dark and light schemes.

        ``_theme_mix`` is 0 while the panel sits on a dark background (light text)
        and 1 on a light one (dark text); it transitions smoothly, so the text
        eases between black and white instead of flipping.
        """
        window = self.config.window
        pair = {
            CURRENT: (window.color_current, window.light_color_current),
            TRANSLATION: (window.color_translation, window.light_color_translation),
        }.get(role, (window.color_lyrics, window.light_color_lyrics))
        dark = parse_color(pair[0])
        light = parse_color(pair[1])
        mix = self._theme_mix
        return tuple(a + (b - a) * mix for a, b in zip(dark, light))

    def _halo(self):
        window = self.config.window
        dark = parse_color(window.halo_color)
        light = parse_color(window.light_halo_color)
        mix = self._theme_mix
        return tuple(a + (b - a) * mix for a, b in zip(dark, light))

    def _status_rows(self) -> list[VisualRow]:
        if not self.status.strip():
            return []
        settings = self.config.window
        size = self._font(settings.font_lyrics)
        lines = self.status.splitlines()
        # Measure each line as it is drawn rather than stepping by the font size:
        # a title long enough to wrap takes two visual lines, and a fixed step
        # would land the line after it on top of that second row -- which is what
        # the notice shown for a song with no lyric did, its lines collapsing into
        # each other.
        heights = [self._drawn_height(text, size, size) or float(size) for text in lines]
        total = sum(heights) + settings.line_spacing * (len(lines) - 1)
        top = self._panel[1] * settings.anchor_ratio - total / 2
        rows = []
        y = top
        for i, text in enumerate(lines):
            rows.append(
                VisualRow(
                    f"status\x00{i}",
                    text,
                    CURRENT,
                    size,
                    y,
                    1.0,
                    attention=True,
                    sharp=1.0,
                )
            )
            y += heights[i] + settings.line_spacing
        return rows

    def _unsynced_rows(self) -> list[VisualRow]:
        settings = self.config.window
        size = self._font(settings.font_lyrics)
        rows = []
        top = float(settings.padding)
        for i, line in enumerate(self.lyrics.lines):
            height = self._layout(line.text, size).get_pixel_size()[1]
            if top + height > self._panel[1] - settings.padding:
                break
            rows.append(
                VisualRow(
                    f"plain\x00{i}",
                    line.text,
                    CURRENT,
                    size,
                    top,
                    1.0,
                    attention=True,
                    sharp=1.0,
                )
            )
            top += height + settings.line_spacing
        return rows

    def _draw_rows(self) -> list[VisualRow]:
        if self._from_rows is None or self._progress >= 1.0:
            return self._settled_rows(-1 if self.index is None else self.index)
        return _interpolate(self._from_rows, self._to_rows, self._progress)

    # -- light/dark theme -------------------------------------------------

    def _set_theme(self, dark: bool) -> None:
        """Aim the text colour at the scheme that suits the sampled background."""
        target = 0.0 if dark else 1.0
        if not self._theme_ready:
            self._theme_ready = True
            self._theme_target = self._theme_mix = target
            # The first decision is applied whole, with no fade to repaint for it,
            # so it has to ask for a frame of its own -- otherwise the panel keeps
            # the colour it was first drawn with. Nothing else redraws when there
            # are no lyrics to sweep, so the notice for a song with no lyric stayed
            # white on a light background until something happened to trigger a
            # repaint.
            self.area.queue_draw()
            return
        if target == self._theme_target:
            return
        self._theme_target = target
        self._theme_from = self._theme_mix
        self._theme_start = time.monotonic()
        self._theme_duration = THEME_FADE_MS / 1000.0
        if not self._theme_timer:
            self._theme_timer = GLib.timeout_add(THEME_FRAME_MS, self._theme_frame)

    def _theme_frame(self) -> bool:
        elapsed = 0.0 if not self._theme_duration else (
            time.monotonic() - self._theme_start
        ) / self._theme_duration
        if elapsed >= 1.0:
            self._theme_mix = self._theme_target
            self._theme_timer = 0
            self.area.queue_draw()
            return False
        self._theme_mix = self._theme_from + (self._theme_target - self._theme_from) * _ease(elapsed)
        self.area.queue_draw()
        return True

    def _on_draw(self, _widget, cr) -> bool:
        settings = self.config.window
        width, height = self._panel
        margin = self._margin()

        cr.set_operator(cairo.Operator.SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(cairo.Operator.OVER)

        self._paint_frame(cr)

        cr.save()
        cr.translate(margin, margin)          # the surface carries shadow margin
        cr.save()
        _rounded_rect(cr, 1, 1, width - 2, height - 2, max(0.0, settings.corner_radius - 1))
        cr.clip()

        # Each row is drawn through the blur and/or crisp in proportion to its
        # `sharp` factor, so a line softens as it leaves the centre and sharpens as
        # it arrives instead of snapping between the two.
        rows = self._draw_rows()
        blurred = [
            replace(row, alpha=row.alpha * (1.0 - row.sharp))
            for row in rows
            if row.alpha * (1.0 - row.sharp) > INVISIBLE
        ]
        crisp = [
            replace(row, alpha=row.alpha * row.sharp)
            for row in rows
            if row.alpha * row.sharp > INVISIBLE
        ]
        if blurred:
            self._draw_blurred(cr, blurred, width, height)
        for row in crisp:
            self._draw_row(cr, row)

        self._fade_edges(cr, width, height, settings.edge_fade)
        self._draw_status_light(cr, height)
        cr.restore()
        cr.restore()
        return True

    def _draw_status_light(self, cr, height: int) -> None:
        """The corner light: red while a lookup is out, green once it is answered.

        Drawn after the edge fade, so the fade does not dim it -- it is a signal
        rather than part of the lyric -- and inside the panel's clip, so it cannot
        stray out past the rounded corner.
        """
        # A rounder corner reaches further into the panel, and the light stands
        # off it by enough to stay whole: an arc of radius r comes r(1 - 1/sqrt 2)
        # in from the corner, and the light's own radius sits on top of that.
        inset = max(
            STATUS_LIGHT_INSET,
            self.config.window.corner_radius * 0.3 + STATUS_LIGHT_RADIUS,
        )
        red, green, blue = STATUS_BUSY_RGB if self.busy else STATUS_IDLE_RGB
        cr.set_source_rgba(red, green, blue, 1.0)
        cr.arc(inset, height - inset, STATUS_LIGHT_RADIUS, 0.0, 2 * math.pi)
        cr.fill()

    def _draw_blurred(self, cr, rows, width, height) -> None:
        blur_px = max(0, int(self.config.window.blur_px))
        if blur_px <= 0:
            for row in rows:
                self._draw_row(cr, row)
            return

        # The layer is kept at the panel's own size, upscaled once when it is
        # rebuilt, so this is a copy rather than a scaled rasterisation: painting a
        # half-size one through a transform cost 0.25 ms a frame against this
        # 0.05, and the upscale it saves is paid once per line rather than per
        # frame.
        cr.set_source_surface(self._blurred_layer(rows, width, height, blur_px), 0, 0)
        cr.paint()

    def _blurred_layer(self, rows, width: int, height: int, blur_px: int):
        """The blurred layer, rebuilt only when what goes into it changes.

        While a line plays it does not change at all: the line being sung is
        drawn crisp and takes no part in the blur, and the neighbours sit still.
        Rasterising and blurring this every frame cost about 30% of a core for a
        picture that was identical each time.
        """
        key = (
            width, height, blur_px, round(self._theme_mix, 4),
            tuple(
                (row.key, row.text, row.role, round(row.size, 2), round(row.y, 2),
                 round(row.alpha, 4), round(row.sharp, 4))
                for row in rows
            ),
        )
        if key != self._blur_key:
            self._blur_surface = self._render_blur(rows, width, height, blur_px)
            self._blur_key = key
        return self._blur_surface

    def _render_blur(self, rows, width: int, height: int, blur_px: int):
        small_w = max(1, width // BLUR_DOWNSCALE)
        small_h = max(1, height // BLUR_DOWNSCALE)
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, small_w, small_h)
        ctx = cairo.Context(surface)
        ctx.scale(1.0 / BLUR_DOWNSCALE, 1.0 / BLUR_DOWNSCALE)
        for row in rows:
            self._draw_row(ctx, row)
        surface.flush()

        width_bytes = small_w * 4
        stride = surface.get_stride()
        raw = np.frombuffer(surface.get_data(), dtype=np.uint8)
        pixels = raw[: stride * small_h].reshape(small_h, stride)[:, :width_bytes].reshape(
            small_h, small_w, 4
        )
        # the downscale is itself part of the blur, so the box radius stays small
        # a radius of zero leaves the box passes out and lets the downscale do the
        # softening, which is the lightest step -- so blur_px moves in small
        # increments instead of jumping by whole tap widths
        radius = max(0, int(round(blur_px / (1.25 * BLUR_DOWNSCALE))))
        blurred = pixels.astype(np.float32)
        for _ in range(BLUR_PASSES):
            blurred = _box_blur(blurred, radius, 0)
            blurred = _box_blur(blurred, radius, 1)
        buffer = np.ascontiguousarray(np.clip(blurred, 0, 255).astype(np.uint8))
        self._blur_buffer = buffer               # cairo reads it, so keep it alive
        small = cairo.ImageSurface.create_for_data(
            buffer, cairo.FORMAT_ARGB32, small_w, small_h, width_bytes
        )

        # Up to the panel's size once, here, so that a frame can paint the layer
        # as it stands. The blurring itself still runs on the small buffer, where
        # the box passes are four times cheaper.
        layer = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
        ctx = cairo.Context(layer)
        ctx.scale(width / small_w, height / small_h)
        pattern = cairo.SurfacePattern(small)
        pattern.set_filter(cairo.Filter.BILINEAR)
        ctx.set_source(pattern)
        ctx.paint()
        layer.flush()
        return layer

    def _paint_frame(self, cr) -> None:
        """Put the border and its glow on, from a layer drawn once.

        They depend on nothing but the settings and the panel's size, so they are
        the same picture on every frame -- and there are 125 of those a second.
        Drawing them each time meant eleven stroked rounded rectangles per frame,
        measured at 0.59 ms of the 1.19 ms a frame cost. Painting the layer
        instead is 0.025 ms, because a surface with no transform over it is a
        copy rather than a rasterisation.
        """
        if self._frame is None:
            self._frame = self._render_frame()
        cr.set_source_surface(self._frame, 0, 0)
        cr.paint()

    def _render_frame(self) -> cairo.ImageSurface:
        width, height = self._panel
        margin = self._margin()
        surface = cairo.ImageSurface(
            cairo.FORMAT_ARGB32, width + 2 * margin, height + 2 * margin
        )
        cr = cairo.Context(surface)
        cr.translate(margin, margin)
        self._draw_frame(cr, width, height)
        surface.flush()
        return surface

    def _draw_frame(self, cr, width: int, height: int) -> None:
        settings = self.config.window
        radius = max(0.0, settings.corner_radius)
        border_width = max(0.0, settings.border_width)
        glow = max(0, int(settings.glow_size))

        if glow and settings.glow_color:
            red, green, blue, base_alpha = parse_color(settings.glow_color)
            total = glow * (glow + 1) / 2
            for layer in range(glow, 0, -1):
                cr.set_source_rgba(red, green, blue, base_alpha * (glow - layer + 1) / total)
                _rounded_rect(
                    cr,
                    border_width / 2 - layer,
                    border_width / 2 - layer,
                    width - border_width + 2 * layer,
                    height - border_width + 2 * layer,
                    radius + layer,
                )
                cr.set_line_width(1)
                cr.stroke()

        if border_width > 0 and settings.border_color:
            cr.set_source_rgba(*parse_color(settings.border_color))
            cr.set_line_width(border_width)
            _rounded_rect(
                cr,
                border_width / 2,
                border_width / 2,
                width - border_width,
                height - border_width,
                radius,
            )
            cr.stroke()

    def _fade_edges(self, cr, width: int, height: int, inset: int) -> None:
        """Fade the lyrics out towards the top and bottom edges of the panel.

        DEST_OUT erases in proportion to the gradient's alpha, so each edge needs
        its opaque end on the panel edge itself and its transparent end `inset`
        pixels inwards.
        """
        if inset <= 0:
            return
        cr.set_operator(cairo.Operator.DEST_OUT)
        for y0, y1, start_alpha, end_alpha in (
            (0, inset, 1.0, 0.0),
            (height - inset, height, 0.0, 1.0),
        ):
            gradient = cairo.LinearGradient(0, y0, 0, y1)
            gradient.add_color_stop_rgba(0.0, 0, 0, 0, start_alpha)
            gradient.add_color_stop_rgba(1.0, 0, 0, 0, end_alpha)
            cr.set_source(gradient)
            cr.rectangle(0, y0, width, y1 - y0)
            cr.fill()
        cr.set_operator(cairo.Operator.OVER)

    def _text_surface(self, text: str, size: int):
        """White glyphs for ``text`` at ``size``, rendered once and cached.

        Every row is rasterised at the size it has when it is the current line and
        then blitted scaled. That keeps the animated font size smooth while
        leaving cairo only two glyph sizes per line to rasterise, instead of a new
        sub-pixel size every frame.

        Returned with the surface is the geometry of each wrapped visual line --
        ``(left, top, width, height)`` in surface pixels. The karaoke sweep needs
        it, and it is exactly what the cached rasterisation already knows.
        """
        key = (text, size)
        entry = self._text_cache.get(key)
        if entry is not None:
            return entry

        layout = self._layout(text, size)
        # The layout box is the full wrap width and centres its line inside it, so
        # the surface has to be that wide -- sizing it from get_pixel_size() (the
        # used width) clips the right of the text by the centring offset, which is
        # large for short lines.
        width = self._wrap_width()
        height = layout.get_pixel_size()[1]
        pad = max(0, int(self.config.window.halo_radius)) * 2 + 1
        if width < 1 or height < 1:
            entry = (None, 0, 0, 0, [])
        else:
            surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width + 2 * pad, height + 2 * pad)
            ctx = cairo.Context(surface)
            ctx.set_source_rgb(1, 1, 1)
            ctx.move_to(pad, pad)
            PangoCairo.show_layout(ctx, layout)
            surface.flush()
            entry = (surface, width, height, pad, self._visual_lines(layout, width))

        if len(self._text_cache) >= TEXT_CACHE_LIMIT:
            self._text_cache.clear()
        self._text_cache[key] = entry
        return entry

    @staticmethod
    def _visual_lines(layout: Pango.Layout, wrap: int) -> list[tuple[float, float, float, float]]:
        """Where Pango will actually draw each wrapped line, in layout pixels.

        Pango reports a line's extents relative to that line's own origin, so the
        width is read from there while the offset is reconstructed: the layout is
        centred, and its vertical position comes from the cursor rect of the
        line's first character.
        """
        lines = []
        for i in range(layout.get_line_count()):
            line = layout.get_line(i)
            _ink, logical = line.get_pixel_extents()
            if logical.width <= 0:
                continue
            top = layout.index_to_pos(line.get_start_index()).y / Pango.SCALE
            left = (wrap - logical.width) / 2.0
            # where each character starts, so the sweep can be told which one it
            # is inside: the karaoke lift moves a character whole
            start, length = line.get_start_index(), line.get_length()
            bounds = sorted(
                {layout.index_to_pos(index).x / Pango.SCALE for index in range(start, start + length)}
            )
            bounds.append(left + logical.width)
            lines.append((left, top, float(logical.width), float(logical.height), bounds))
        return lines

    def _wipe_rects(self, lines, pad: int, factor: float, progress: float,
                    lift: float = 0.0, span: float = 1.0):
        """What to draw for a row being sung: ``(rect, sung, raise)`` each.

        A line that wrapped is swept row by row, each row taking a share of the
        sweep in proportion to its width, so the sweep continues on the second row
        instead of the whole lower half lighting up at once.

        The colour is a continuous sweep; the lift follows it as closely as it can
        while still moving a character as a whole. Each character rises over the
        span of the sweep that passes through it -- see ``_lift_runs`` -- so the
        raise travels along the line rather than switching on character by
        character, and a character the sweep is inside is neither left split in
        two nor lifted ahead of its turn.

        A raised rectangle is grown upwards by its own raise, or the raised tops
        of the characters would be cut off by their own clip.
        """
        total = sum(line[2] for line in lines)
        if total <= 0:
            return []
        out: list[tuple] = []
        done = 0.0
        for left, top, width, height, bounds in lines:
            share = width / total
            begin = done / total
            done += width
            local = 1.0 if share <= 0 else (progress - begin) / share
            local = max(0.0, min(1.0, local))
            right = left + width
            edge = left + local * width
            top_px = (pad + top) * factor
            height_px = height * factor

            for x0, x1, raised in _lift_runs(bounds, edge, lift, span):
                if x1 - x0 <= 0.0:
                    continue
                for piece0, piece1, sung in (
                    (x0, min(x1, edge), True),
                    (max(x0, edge), x1, False),
                ):
                    if piece1 - piece0 <= 0.0:
                        continue
                    out.append((
                        (
                            (pad + piece0) * factor,
                            top_px - raised,
                            (piece1 - piece0) * factor,
                            height_px + raised,
                        ),
                        sung,
                        raised,
                    ))
        return out

    def _draw_row(self, ctx, row: VisualRow) -> None:
        settings = self.config.window
        full_size = self._font(
            settings.font_translation if row.role == TRANSLATION else settings.font_lyrics
        )
        surface, _width, _height, pad, lines = self._text_surface(row.text, full_size)
        if surface is None:
            return

        factor = row.size / full_size if full_size else 1.0
        # Anchor the blit on the layout box's horizontal centre, not its top-left:
        # the box is the full wrap width and Pango centres the line inside it, so
        # scaling about the top-left would drag a short line off-centre. Vertically
        # the box top is the anchor, matching how the row heights are measured.
        wrap = self._wrap_width()
        box_centre = settings.padding + wrap / 2.0
        x = box_centre - (pad + wrap / 2.0) * factor
        y = row.y - pad * factor

        pattern = cairo.SurfacePattern(surface)
        pattern.set_filter(cairo.Filter.BILINEAR)
        pattern.set_matrix(cairo.Matrix(1.0 / factor, 0, 0, 1.0 / factor, 0, 0))

        radius = max(0, int(settings.halo_radius))
        if radius:
            ctx.set_source_rgba(*self._halo_color(self._halo(), row.alpha))
            for offset in ((radius,) if radius < 2 else (radius, radius / 2)):
                step = math.ceil(offset * factor)
                for dx, dy in HALO_DIRECTIONS:
                    ctx.save()
                    ctx.translate(x + dx * step, y + dy * step)
                    ctx.mask(pattern)
                    ctx.restore()

        wipe = self._row_wipe(row)
        if wipe is None:
            ctx.set_source_rgba(*_tint(self._color(row.role), row.alpha))
            ctx.save()
            ctx.translate(x, y)
            ctx.mask(pattern)
            ctx.restore()
            return

        progress, sweeps = wipe
        shade = max(0.0, min(1.0, settings.unsung_opacity))
        colour = self._color(row.role)
        if not sweeps:
            # the translation is not itself sung: it stays dim until the line is
            # through rather than sweeping along with the words
            ctx.set_source_rgba(*_tint(colour, row.alpha * (shade + (1 - shade) * progress)))
            ctx.save()
            ctx.translate(x, y)
            ctx.mask(pattern)
            ctx.restore()
            return

        # The same mask twice, each side of the sweep clipped away in turn: the
        # edge falls wherever the progress is, so a character it lands inside is
        # lit on one side and dim on the other.
        # What has been sung rides a little above what has not, so the sweep reads
        # as the line lifting as it goes rather than only changing colour. Each
        # piece is clipped first and the mask then drawn from higher up, which
        # keeps them meeting at the same vertical edge. The lift is scaled by how
        # crisp the row is, so a line sinking into the background as it is left
        # behind puts its raised characters down smoothly instead of dropping them.
        # the lift is a pixel count tuned at the default lyric size; it scales with
        # the effective font so it keeps the same share of the glyph height
        lift = (
            max(0.0, float(settings.karaoke_lift))
            * (self._font(settings.font_lyrics) / KARAOKE_LIFT_REFERENCE)
            * row.sharp
        )
        span = max(0.2, float(settings.karaoke_lift_span))
        for piece, sung, raised in self._wipe_rects(lines, pad, factor, progress, lift, span):
            ctx.save()
            ctx.translate(x, y)
            ctx.new_path()
            ctx.rectangle(*piece)
            ctx.clip()
            if raised:
                ctx.translate(0, -raised)
            ctx.set_source_rgba(
                *_tint(colour, row.alpha if sung else row.alpha * shade)
            )
            ctx.mask(pattern)
            ctx.restore()

    def _halo_color(self, halo, fade: float):
        """The outline keeps most of its strength as a line fades.

        It is what makes the text legible over an arbitrary wallpaper, so fading
        it in step with the text would erase distant lines instead of dimming them.
        """
        red, green, blue, _ = halo
        alpha = max(0.0, min(1.0, self.config.window.halo_alpha))
        alpha *= HALO_FADE_FLOOR + (1.0 - HALO_FADE_FLOOR) * max(0.0, min(1.0, fade))
        return red, green, blue, alpha


def _lift_runs(bounds: list[float], edge: float, lift: float, span: float):
    """``(x0, x1, raise)`` runs across one visual line's characters.

    Each character rises as a unit, by however far the sweep has travelled
    through it: nothing while the sweep is still short of it, all of it once the
    sweep is past, and a smoothstep in between. That is what keeps a character
    from being cut in half by the sweep while still arriving gradually.

    ``span`` is how many character widths the rise takes, so a wider one starts
    a character moving before the sweep reaches it and carries on past it, and
    several characters end up in motion together. Runs at the same height are
    merged, which leaves the flat parts of the line as single rectangles.
    """
    runs: list[list[float]] = []
    for index in range(len(bounds) - 1):
        start, end = bounds[index], bounds[index + 1]
        step = max(1.0, end - start) * max(0.2, span)
        progress = max(0.0, min(1.0, (edge - start) / step))
        raise_ = round(lift * progress * progress * (3.0 - 2.0 * progress) * 4.0) / 4.0
        if runs and abs(runs[-1][2] - raise_) < 0.13:
            runs[-1][1] = end
        else:
            runs.append([start, end, raise_])
    return [(x0, x1, raise_) for x0, x1, raise_ in runs]


def timeline_source_of(lyrics, window) -> str | None:
    """Which timing a whole lyric will be sung with, given the settings.

    The first source switched on that covers most of the lyric's lines -- the
    order the menu lists, which is the order the sweep tries -- or None when the
    lyric has none to offer and the average pace is all there is. The name given
    back is the timing's own (``yrc`` for a NetEase answer, ``krc`` for Kugou's,
    ``qrc`` for QQ's), so a log line says which site it was. The menu marks the
    source for the line on screen; this asks the same question of the song, for
    the log.
    """
    if lyrics is None or not lyrics.lines or not window.karaoke:
        return None
    for name, _label, keys in TIMELINE_SOURCES:
        if not getattr(window, "karaoke_" + name):
            continue
        for key in keys:
            if lyrics.timing_covers(key):
                return key
    return None


def _timeline_progress(units, elapsed_ms: float) -> float | None:
    """How much of a line has been sung, as a fraction of its characters.

    ``units`` is the line's own word-level timing -- ``(offset, duration, text)``
    each, offsets measured from the line's start. Counting characters rather than
    time is what puts the sweep on the character being sung: real singing is not
    even, and a ramp over the line's average pace drifts from it by seconds on a
    line with a held note or a rushed phrase.

    None when there is no timing to go by, so the caller falls back to the
    average pace. The units are known to spell out the line -- the parser checks
    that before keeping them -- so their characters are the line's characters.
    """
    if not units:
        return None
    total = sum(len(text) for _offset, _duration, text in units)
    if total <= 0:
        return None
    sung = 0
    for offset, duration, text in units:
        if elapsed_ms < offset:
            break
        if duration <= 0 or elapsed_ms >= offset + duration:
            sung += len(text)
            continue
        return min(1.0, (sung + len(text) * (elapsed_ms - offset) / duration) / total)
    return min(1.0, sung / total)


def _box_blur(values: np.ndarray, radius: int, axis: int) -> np.ndarray:
    """One box-blur pass; three of them approximate a Gaussian (premultiplied
    ARGB blurs correctly because every channel carries its own weight)."""
    if radius < 1:
        return values
    moved = np.moveaxis(values, axis, 0)
    padded = np.pad(
        moved, [(radius, radius)] + [(0, 0)] * (moved.ndim - 1), mode="edge"
    ).astype(np.float32)
    cumulative = np.cumsum(padded, axis=0)
    cumulative = np.concatenate([np.zeros_like(cumulative[:1]), cumulative], axis=0)
    window = 2 * radius + 1
    out = (cumulative[window:] - cumulative[:-window]) / window
    return np.moveaxis(out, 0, axis)


def _median_luma(pixbuf) -> float:
    """The middle brightness of a captured region.

    Every other pixel is used: the panel is large enough that halving the sample
    changes nothing about which side of the middle it lands on, and this runs
    twice a second.
    """
    width, height = pixbuf.get_width(), pixbuf.get_height()
    channels = pixbuf.get_n_channels()
    stride = pixbuf.get_rowstride()
    raw = np.frombuffer(pixbuf.get_pixels(), dtype=np.uint8)
    # PyGObject hands back stride*(height-1) + width*channels bytes, not the full
    # allocation, so give it a full-size backing buffer before reshaping.
    padded = np.zeros(stride * height, dtype=np.uint8)
    padded[: min(raw.size, padded.size)] = raw[: padded.size]
    rows = padded.reshape(height, stride)[:, : width * channels]
    pixels = rows.reshape(height, width, channels)[::2, ::2, :3].astype(np.float32)
    return float(np.median(pixels))


def _interpolate(from_rows: list[VisualRow], to_rows: list[VisualRow], p: float) -> list[VisualRow]:
    """Blend two settled layouts.

    Rows are matched by line number, so the line that was the next one and the
    line that is now current are the same row: it slides up into the pinned
    position while its colour fades up to the highlighted one.
    """
    from_by_key = {row.key: row for row in from_rows}
    to_by_key = {row.key: row for row in to_rows}

    shift = None
    for row in to_rows:
        if row.role == CURRENT and row.key in from_by_key:
            shift = from_by_key[row.key].y - row.y
            break
    if shift is None:
        shift = 0.0

    leaving = [
        replace(row, y=row.y - p * shift, alpha=row.alpha * (1 - p), sharp=row.sharp * (1 - p))
        for key, row in from_by_key.items()
        if key not in to_by_key
    ]
    arriving = [
        replace(row, y=row.y + (1 - p) * shift, alpha=row.alpha * p)
        for key, row in to_by_key.items()
        if key not in from_by_key
    ]
    staying = [
        replace(
            row,
            # Size and position stay fractional: the glyphs are rasterised once per
            # line at its full size and blitted scaled, so a smooth font size costs
            # nothing to animate (rounding these to pixels made the growth visibly
            # step, and re-rasterising them every frame cost 3x the frame time).
            size=old.size + (row.size - old.size) * p,
            y=old.y + (row.y - old.y) * p,
            alpha=old.alpha + (row.alpha - old.alpha) * p,
            sharp=old.sharp + (row.sharp - old.sharp) * p,
        )
        for key, row in to_by_key.items()
        if (old := from_by_key.get(key)) is not None
    ]
    return leaving + arriving + staying


def _rounded_rect(cr, x: float, y: float, width: float, height: float, radius: float) -> None:
    radius = max(0.0, min(radius, width / 2, height / 2))
    cr.new_sub_path()
    cr.arc(x + width - radius, y + radius, radius, -math.pi / 2, 0)
    cr.arc(x + width - radius, y + height - radius, radius, 0, math.pi / 2)
    cr.arc(x + radius, y + height - radius, radius, math.pi / 2, math.pi)
    cr.arc(x + radius, y + radius, radius, math.pi, 3 * math.pi / 2)
    cr.close_path()


def _ease(t: float) -> float:
    return 1.0 - (1.0 - t) ** 3


def _tint(color, alpha: float):
    red, green, blue, base_alpha = color
    return red, green, blue, base_alpha * max(0.0, min(1.0, alpha))


def parse_color(value: str, default_alpha: float = 1.0):
    digits = (value or "").strip().lstrip("#")
    if len(digits) == 6:
        digits += "FF"
    if len(digits) != 8:
        return 1.0, 1.0, 1.0, default_alpha
    try:
        return tuple(int(digits[i : i + 2], 16) / 255 for i in (0, 2, 4, 6))
    except ValueError:
        return 1.0, 1.0, 1.0, default_alpha
