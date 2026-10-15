"""LX Music (lx-music-desktop) adapter.

LX Music exposes no MPRIS interface on Linux, but it has a built-in "open API"
that reports the current song and serves the synced lyric it is displaying. It
is off by default; the user switches it on in LX Music's own settings, and it
listens on 127.0.0.1 only unless "bind to LAN" is enabled.
"""

from __future__ import annotations

import time
from pathlib import Path

import requests
from gi.repository import GLib, GObject

from .mpris import Track, TrackSignals

STATUS_PATH = "/status"
LYRIC_PATH = "/lyric"
TIMEOUT = 2.0
PROCESS_NAMES = ("lx-music-desktop", "lx-music")


class LxMusicPlayer(GObject.Object):
    """Presents LX Music's HTTP API with the same surface as the MPRIS client."""

    __gsignals__ = TrackSignals

    def __init__(self, port: int = 23330, poll_ms: int = 500) -> None:
        super().__init__()
        self.key = "lxmusic"
        self.label = "LX Music"
        self.base_url = f"http://127.0.0.1:{port}"
        self.poll_ms = poll_ms
        self.session = requests.Session()
        self.track = Track()
        self.status = "Stopped"
        self.connected = False
        self._progress_us = 0
        self._rate = 1.0
        self._lyric: str | None = None
        self._lyric_for: str | None = None
        self._timer = 0
        self._pid: int | None = None

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        self._poll()
        if not self._timer:
            self._timer = GLib.timeout_add(self.poll_ms, self._poll)

    def stop(self) -> None:
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0

    def suspend(self) -> None:
        """Stand down: another source is speaking for this player."""
        if not self.connected:
            return
        self.stop()
        self.connected = False
        self.track = Track()
        self.emit("disconnected")

    # -- polling ---------------------------------------------------------

    def _poll(self) -> bool:
        try:
            payload = self.session.get(self.base_url + STATUS_PATH, timeout=TIMEOUT).json()
        except (requests.RequestException, ValueError):
            if self.connected:
                self.connected = False
                self.track = Track()
                self.emit("disconnected")
            return True

        if not self.connected:
            self.connected = True
            self.emit("connected")

        track = Track(
            title=str(payload.get("name") or ""),
            artist=str(payload.get("singer") or ""),
            album=str(payload.get("albumName") or ""),
            length_ms=int(float(payload.get("duration") or 0) * 1000),
        )
        if not track.same_song(self.track):
            self.track = track
            self._lyric = None
            self._lyric_for = None
            self.emit("track-changed")

        self._progress_us = int(float(payload.get("progress") or 0) * 1_000_000)
        self._rate = float(payload.get("playbackRate") or 1.0)
        status = {"playing": "Playing", "paused": "Paused"}.get(
            str(payload.get("status") or "").lower(), "Stopped"
        )
        if status != self.status:
            self.status = status
            self.emit("state-changed")
        return True

    # -- the surface the app expects -------------------------------------

    def position_us(self) -> int | None:
        return self._progress_us if self.connected else None

    def rate(self) -> float:
        return self._rate

    def player_pid(self) -> int | None:
        if self._pid is not None and Path(f"/proc/{self._pid}").exists():
            return self._pid
        self._pid = _find_process(PROCESS_NAMES)
        return self._pid

    def lyric_text(self) -> str | None:
        """The lyric LX Music is showing, which matches its own clock exactly.

        Called from the lookup worker thread, so it deliberately does not share
        the session the polling uses.
        """
        key = f"{self.track.title}\x00{self.track.artist}"
        if self._lyric_for == key:
            return self._lyric
        self._lyric_for = key
        self._lyric = None
        try:
            text = requests.get(self.base_url + LYRIC_PATH, timeout=TIMEOUT).text
        except requests.RequestException:
            return None
        if text and text.lstrip().startswith("["):
            self._lyric = text
        return self._lyric


def _find_process(names: tuple[str, ...]) -> int | None:
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            command = (entry / "comm").read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if command in names:
            return int(entry.name)
    return None
