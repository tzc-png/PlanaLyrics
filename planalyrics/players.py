"""Which playback source the lyrics follow.

MyUne Music and browsers announce themselves on the session bus as MPRIS
players; LX Music does not, but serves its own HTTP API. The hub discovers
whatever is available, follows whichever source is playing, and lets the user
pin one by hand from the overlay's menu.
"""

from __future__ import annotations

import time

from gi.repository import Gio, GLib, GObject

from .lxmusic import LxMusicPlayer
from .mpris import MprisClient, Track, TrackSignals

MPRIS_PREFIX = "org.mpris.MediaPlayer2."
RESCAN_MS = 1000


class PlayerHub(GObject.Object):
    __gsignals__ = TrackSignals

    def __init__(self, pin: str = "", lxmusic_port: int = 23330, poll_ms: int = 500) -> None:
        super().__init__()
        self.pin = pin
        self.lxmusic_port = lxmusic_port
        self.poll_ms = poll_ms
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.sources: dict[str, GObject.Object] = {}
        self.active: GObject.Object | None = None
        self._playing_since: dict[str, float] = {}
        self._timer = 0

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        self._scan()
        if not self._timer:
            self._timer = GLib.timeout_add(RESCAN_MS, self._tick)

    def _tick(self) -> bool:
        self._scan()
        self._choose()
        return True

    def _scan(self) -> None:
        try:
            names = self.bus.call_sync(
                "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                "ListNames", None, None, Gio.DBusCallFlags.NONE, 1000, None,
            ).unpack()[0]
        except GLib.Error:
            names = []
        for name in names:
            if name.startswith(MPRIS_PREFIX):
                self._add_mpris(name)
        self._add_lxmusic(
            any(name.startswith(MPRIS_PREFIX) for name in names)
        )

    def _add_mpris(self, name: str) -> None:
        if name in self.sources:
            return
        client = MprisClient(name)
        self._track(client)
        client.start()

    def _add_lxmusic(self, bus_has_mpris: bool) -> None:
        """LX Music's HTTP API is only a fallback.

        LX Music normally reaches us as a Chromium MPRIS player (Electron
        publishes one), and offering the same player twice would be confusing, so
        the API adapter only joins in when nothing on the bus speaks MPRIS.
        """
        existing = self.sources.get("lxmusic")
        if bus_has_mpris:
            if existing is not None:
                existing.suspend()
            return
        if existing is None:
            existing = LxMusicPlayer(port=self.lxmusic_port, poll_ms=self.poll_ms)
            self._track(existing)
        existing.start()

    def _track(self, source: GObject.Object) -> None:
        self.sources[source.key] = source
        for signal in ("track-changed", "state-changed", "connected", "disconnected"):
            source.connect(signal, lambda emitter, name=signal: self._on_source_signal(emitter, name))
        source.connect("seeked", self._on_source_seeked)

    # -- selection -------------------------------------------------------

    def _on_source_signal(self, source, signal: str) -> None:
        if signal == "state-changed" and source.status == "Playing":
            self._playing_since[source.key] = time.monotonic()
        self._choose()
        self._forward(source, signal)

    def _on_source_seeked(self, source, position_us: int) -> None:
        self._forward(source, "seeked", (position_us,))

    def _forward(self, source, signal: str, args: tuple = ()) -> None:
        if source is self.active:
            self.emit(signal, *args)

    def _choose(self) -> None:
        connected = [s for s in self.sources.values() if getattr(s, "connected", False)]
        if not connected:
            self._set_active(None)
            return

        if self.pin:
            match = next((s for s in connected if s.key == self.pin), None)
            if match is not None:
                self._set_active(match)
                return

        playing = [s for s in connected if s.status == "Playing"]
        if playing:
            if self.active in playing:
                return                       # stay put: switching would flap
            newest = max(playing, key=lambda s: self._playing_since.get(s.key, 0.0))
            self._set_active(newest)
            return
        self._set_active(self.active if self.active in connected else connected[0])

    def _set_active(self, source: GObject.Object | None) -> None:
        if source is self.active:
            return
        self.active = source
        if source is None:
            self.emit("disconnected")
        else:
            self.emit("connected")
            self.emit("track-changed")

    # -- manual control --------------------------------------------------

    def select(self, key: str | None) -> None:
        """Follow ``key`` from now on, or None to follow whatever is playing.

        The pin is the one place this is held, so writing it to the settings file
        leaves nothing to drift out of step.
        """
        self.pin = key or ""
        self._choose()

    def available(self) -> list[tuple[str, str, bool]]:
        return sorted(
            (
                (s.key, s.label, getattr(s, "status", "") == "Playing")
                for s in self.sources.values()
                if getattr(s, "connected", False)
            ),
            key=lambda item: item[1].lower(),
        )

    def active_key(self) -> str:
        return self.active.key if self.active is not None else ""

    def pinned_key(self) -> str:
        """The player the user pinned, or "" when following whatever plays."""
        return self.pin or ""

    # -- the surface the app uses ----------------------------------------

    @property
    def connected(self) -> bool:
        return self.active is not None and getattr(self.active, "connected", False)

    @property
    def track(self) -> Track:
        return self.active.track if self.active is not None else Track()

    @property
    def status(self) -> str:
        return self.active.status if self.active is not None else "Stopped"

    def position_us(self) -> int | None:
        return self.active.position_us() if self.active is not None else None

    def rate(self) -> float:
        return self.active.rate() if self.active is not None else 1.0

    def player_pid(self) -> int | None:
        return self.active.player_pid() if self.active is not None else None

    def lyric_text(self) -> str | None:
        """Lyrics supplied by the player itself, when it can (LX Music does)."""
        if self.active is None:
            return None
        provider = getattr(self.active, "lyric_text", None)
        return provider() if callable(provider) else None
