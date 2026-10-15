"""Wiring: MPRIS in, timing and lookup in the middle, overlay out."""

from __future__ import annotations

import signal
import threading
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")

from gi.repository import Gio, GLib, Gtk  # noqa: E402

from . import config as config_mod  # noqa: E402
from . import locate, sources  # noqa: E402
from .config import Config  # noqa: E402
from .lyrics import Lyrics  # noqa: E402
from .mpris import Track  # noqa: E402
from .overlay import Overlay  # noqa: E402
from .players import PlayerHub  # noqa: E402

TICK_MS = 100
POLL_MS = 500
# how long a lookup may take before the panel says it is looking: a lyric that
# is already on disk lands in a few milliseconds, and announcing the search
# immediately means a flash of title-and-artist between two songs
LOOKUP_NOTICE_MS = 300
# a backward step this large is a real seek; anything smaller is the player
# repeating a stale Position value and must not drag playback back
SEEK_BACKWARD_TOLERANCE_US = 1_500_000
WAITING = "等待 MyUne Music…"


class App:
    def __init__(self, config: Config | None = None) -> None:
        self.config = config or config_mod.load()
        self.players = PlayerHub(
            pin=self.config.player.pin,
            lxmusic_port=self.config.player.lxmusic_port,
            poll_ms=self.config.player.poll_ms,
        )
        self.players.connect("connected", self._on_connected)
        self.players.connect("disconnected", self._on_disconnected)
        self.players.connect("track-changed", self._on_track_changed)
        self.players.connect("state-changed", self._on_state_changed)
        self.players.connect("seeked", self._on_seeked)

        self.track: Track | None = None
        self.lyrics: Lyrics | None = None
        self.playing = False
        self.anchor_us = 0
        self.anchor_time = time.monotonic()
        self.generation = 0
        self._notice_timer = 0

        # the overlay's menu lists the players, so it is built after the hub
        self.overlay = Overlay(self.config, self)
        self.players.start()
        self._config_monitor = config_mod.watch(self._on_config_file_changed)
        self.overlay.set_status(WAITING)
        GLib.timeout_add(TICK_MS, self._tick)
        GLib.timeout_add(POLL_MS, self._poll)

    # -- lifecycle -------------------------------------------------------

    def run(self) -> int:
        Gtk.main()
        return 0

    def quit(self) -> None:
        config_mod.save_state(self.config, panel=self.overlay.panel_size())
        Gtk.main_quit()

    def on_window_moved(self, x: int, y: int) -> None:
        self.config.window.x = x
        self.config.window.y = y
        config_mod.save_state(self.config, panel=self.overlay.panel_size())

    def on_panel_resized(self, width: int, height: int) -> None:
        config_mod.save_state(self.config, panel=(width, height))

    def open_config_folder(self) -> None:
        """Show config.toml (the defaults) and state.json (what the app remembers)."""
        try:
            Gio.AppInfo.launch_default_for_uri(config_mod.CONFIG_DIR.as_uri(), None)
        except GLib.Error as exc:
            print(f"PlanaLyrics: 打不开配置文件夹 {config_mod.CONFIG_DIR} ({exc.message})")

    # -- which player the lyrics follow ----------------------------------

    def player_options(self) -> list[tuple[str, str, bool]]:
        return self.players.available()

    def active_player(self) -> str:
        return self.players.active_key()

    def pinned_player(self) -> str:
        return self.players.pinned_key()

    def select_player(self, key: str | None) -> None:
        """Pin a player (or None to go back to following whatever is playing)."""
        self.players.select(key)
        self.save_setting("player", "pin", key or "")

    def save_setting(self, section: str, key: str, value: object) -> None:
        """Write a setting changed from the menu into config.toml.

        The menu is a way of editing the file, so what it changes has to survive
        the run -- otherwise a setting would quietly revert on restart.
        """
        config_mod.save_values({section: {key: value}})

    def restore_defaults(self) -> None:
        """Put the shipped defaults back, in the file and on screen."""
        if not config_mod.restore_defaults():
            return
        self.reload_config(notify=False)
        print("PlanaLyrics: 已恢复默认设置")

    # -- config ----------------------------------------------------------

    def _on_config_file_changed(self) -> None:
        GLib.timeout_add(300, self._reload_config_idle)

    def _reload_config_idle(self) -> bool:
        self.reload_config(notify=False)
        return False

    def reload_config(self, notify: bool = True) -> None:
        self.config = config_mod.load()
        self.overlay.apply_config(self.config)
        self.overlay.force_redraw()
        # the lyrics settings decide which sources apply, so they take effect the
        # same way the rest of the file does
        self.reload_lyrics()
        if notify:
            print("PlanaLyrics: 配置已重载")

    def research(self) -> None:
        if self.track is not None:
            self._start_lookup(self.track, use_cache=False)

    def reload_lyrics(self) -> None:
        """Re-resolve the current track, reusing what is already known.

        For a setting that changes which sources apply rather than asking for a
        fresh answer from the network.
        """
        if self.track is not None:
            self._start_lookup(self.track)

    def adjust_offset(self, delta_ms: int | None) -> None:
        self.config.lyrics.offset_ms = 0 if delta_ms is None else self.config.lyrics.offset_ms + delta_ms
        self.save_setting("lyrics", "offset_ms", self.config.lyrics.offset_ms)
        self.overlay.force_redraw()
        print(f"PlanaLyrics: 歌词偏移 {self.config.lyrics.offset_ms} ms")

    # -- MPRIS events ----------------------------------------------------

    def _on_connected(self, *_args) -> None:
        self.overlay.set_status("等待播放…")
        self._poll()

    def _on_disconnected(self, *_args) -> None:
        self.track = None
        self.lyrics = None
        self.generation += 1
        self.overlay.set_status(WAITING)

    def _on_state_changed(self, *_args) -> None:
        self._set_playing(self.players.status == "Playing")
        self._reanchor(self.players.position_us())

    def _on_track_changed(self, *_args) -> None:
        track = self.players.track
        if not track.title:
            return
        if self.track is not None and track.same_song(self.track):
            return
        self.track = track
        self.lyrics = None
        self.playing = self.players.status == "Playing"
        self._reanchor(self.players.position_us(), clamp=False)   # a new track: trust it
        self._start_lookup(track)

    def _on_seeked(self, _client, position_us: int) -> None:
        self.anchor_us = position_us
        self.anchor_time = time.monotonic()

    # -- lookup ----------------------------------------------------------

    def _start_lookup(self, track: Track, use_cache: bool = True) -> None:
        self.generation += 1
        generation = self.generation
        # Only admit to searching if it takes a moment. A lyric that is already
        # on disk arrives in a few milliseconds, and the notice would be a flash
        # of title-and-artist between the old song's last line and the new
        # song's first -- the previous lyrics stay up until then, which reads as
        # one song handing over to the next rather than as a jump.
        if self._notice_timer:
            GLib.source_remove(self._notice_timer)
        self._notice_timer = GLib.timeout_add(
            LOOKUP_NOTICE_MS, self._show_lookup_notice, track, generation
        )
        threading.Thread(
            target=self._lookup_worker,
            args=(track, generation, use_cache),
            daemon=True,
        ).start()

    def _show_lookup_notice(self, track: Track, generation: int) -> bool:
        self._notice_timer = 0
        if generation == self.generation:
            self.overlay.set_status(f"{track.title}\n{track.artist}\n正在查找歌词…")
        return False

    def _lookup_worker(self, track: Track, generation: int, use_cache: bool) -> None:
        path = None
        lyrics = None
        try:
            path = locate.locate(
                track,
                [Path(d).expanduser() for d in self.config.lyrics.music_dirs],
                self.config.lyrics.resolved_cache_dir,
                self.players.player_pid(),
            )
            lyrics = sources.resolve(
                path,
                track,
                self.config.lyrics.resolved_cache_dir,
                allow_online=self.config.lyrics.online_enabled,
                use_cache=use_cache,
                preferred_text=self.players.lyric_text(),
                search_translation=self.config.lyrics.search_translation,
            )
        except Exception as exc:  # a failed lookup must not kill the overlay
            print(f"PlanaLyrics: 查找歌词失败 ({exc})")
        GLib.idle_add(self._apply_lookup, track, generation, path, lyrics)

    def _apply_lookup(self, track: Track, generation: int, path: Path | None, lyrics: Lyrics | None) -> bool:
        if generation != self.generation:
            return False
        # the answer is here, so the "looking for lyrics" notice is not wanted
        if self._notice_timer:
            GLib.source_remove(self._notice_timer)
            self._notice_timer = 0
        if lyrics is not None and lyrics.lines:
            self.lyrics = lyrics
            self.overlay.set_lyrics(lyrics)
            print(f"PlanaLyrics: {track.title} -> {lyrics.source or '内嵌'} ({len(lyrics.lines)} 行)")
        else:
            self.lyrics = None
            if self.config.lyrics.online_enabled:
                where = path.name if path else "找不到本地文件"
                note = f"无歌词（{where}）"
            else:
                # saying which is missing matters when the search is off: nothing
                # is going to turn up on its own
                note = "本地无歌词（联网搜索已关闭）"
            self.overlay.set_status(f"{track.title}\n{track.artist}\n{note}")
            print(f"PlanaLyrics: {track.title} {note}")
        return False

    # -- timing ----------------------------------------------------------

    def _estimate_us(self, playing: bool | None = None) -> int:
        """Playback position interpolated from the last anchor."""
        if playing is None:
            playing = self.playing
        if not playing:
            return self.anchor_us
        return self.anchor_us + int((time.monotonic() - self.anchor_time) * 1_000_000)

    def _set_playing(self, playing: bool) -> None:
        """Adopt the player's playing state, pinning the clock where it stands.

        While playing, the position on screen has been interpolated forward from
        the player's last report; that report is the same value, up to a poll
        interval older. So when the player pauses it is the position on screen
        that is right, and letting the report win instead drags the lyrics -- and
        the karaoke sweep -- backwards on every pause, and then forwards again on
        resume. Re-anchoring under the *old* state keeps whatever the clock had
        reached, and restarts the interpolation from there.
        """
        was_playing = self.playing
        if playing != was_playing:
            self._reanchor(self._estimate_us(playing=was_playing), clamp=False)
        self.playing = playing

    def _reanchor(self, position_us: int | None, clamp: bool = True) -> None:
        """Adopt the player's reported position, ignoring small backward steps.

        MyUne Music repeats the same Position value for two polls in a row about
        once a second, so taking it verbatim drags playback back ~500 ms. Landing
        that across a line boundary is what makes the lyrics flick forward and
        then jump back to the previous line. A large backward jump is a real seek
        and is adopted as-is; seeks also arrive on their own signal.
        """
        if position_us is None:
            position_us = self.anchor_us
        if clamp:
            estimate = self._estimate_us()
            if position_us >= estimate - SEEK_BACKWARD_TOLERANCE_US:
                position_us = max(position_us, estimate)
        self.anchor_us = position_us
        self.anchor_time = time.monotonic()

    def _poll(self) -> bool:
        if self.players.connected:
            self._set_playing(self.players.status == "Playing")
            self._reanchor(self.players.position_us())
        return True

    def _tick(self) -> bool:
        if self.lyrics is not None and self.lyrics.synced:
            t_ms = self._estimate_us() / 1000.0 + self.config.lyrics.offset_ms
            # Before the first timestamp there is no line to be on, so the first
            # one waits in the centre rather than sitting below an empty middle.
            # Its sweep starts from zero and does not move until its own moment
            # arrives, which is exactly what a negative progress works out to.
            self.overlay.set_index(max(0, self.lyrics.index_at(int(t_ms))))
            # hand the overlay the clock as well as the line: it reads it forward
            # between ticks so the karaoke sweep moves at frame rate
            self.overlay.set_position(t_ms, self.playing)
        return True


def main() -> int:
    GLib.set_prgname("planalyrics")
    GLib.set_application_name("PlanaLyrics")
    Gtk.init()
    app = App()
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, lambda *_: (app.quit(), True)[1])
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, lambda *_: (app.quit(), True)[1])
    return app.run()
