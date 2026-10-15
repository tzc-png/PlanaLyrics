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
        config_mod.save_state(self.config, panel=self.overlay.panel_size())
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
        self.playing = self.players.status == "Playing"
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
        self.overlay.set_status(f"{track.title}\n{track.artist}\n正在查找歌词…")
        threading.Thread(
            target=self._lookup_worker,
            args=(track, generation, use_cache),
            daemon=True,
        ).start()

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
        if lyrics is not None and lyrics.lines:
            self.lyrics = lyrics
            self.overlay.set_lyrics(lyrics)
            print(f"PlanaLyrics: {track.title} -> {lyrics.source or '内嵌'} ({len(lyrics.lines)} 行)")
        else:
            self.lyrics = None
            where = path.name if path else "找不到本地文件"
            self.overlay.set_status(f"{track.title}\n{track.artist}\n无歌词（{where}）")
            print(f"PlanaLyrics: {track.title} 无歌词 ({where})")
        return False

    # -- timing ----------------------------------------------------------

    def _estimate_us(self) -> int:
        """Playback position interpolated from the last anchor."""
        if not self.playing:
            return self.anchor_us
        return self.anchor_us + int((time.monotonic() - self.anchor_time) * 1_000_000)

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
            was_playing = self.playing
            self.playing = self.players.status == "Playing"
            self._reanchor(self.players.position_us(), clamp=was_playing)
        return True

    def _tick(self) -> bool:
        if self.lyrics is not None and self.lyrics.synced:
            elapsed_us = 0
            if self.playing:
                elapsed_us = int((time.monotonic() - self.anchor_time) * 1_000_000)
            t_ms = (self.anchor_us + elapsed_us) / 1000.0 + self.config.lyrics.offset_ms
            self.overlay.set_index(self.lyrics.index_at(int(t_ms)))
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
