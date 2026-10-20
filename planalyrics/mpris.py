"""MPRIS client for the running music player.

Uses Gio.DBusProxy rather than python-dbus: its ``g-properties-changed``
signal works without wiring a GLib main loop into dbus first.
"""

from __future__ import annotations

from dataclasses import dataclass

from gi.repository import Gio, GLib, GObject

OBJECT_PATH = "/org/mpris/MediaPlayer2"
PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"
APP_IFACE = "org.mpris.MediaPlayer2"
PROPS_IFACE = "org.freedesktop.DBus.Properties"
CALL_TIMEOUT_MS = 1000
# what the overlay's controls ask for, by the name the MPRIS Player interface
# gives each call
CONTROL_METHODS = {
    "playpause": "PlayPause",
    "previous": "Previous",
    "next": "Next",
}

# shared by every playback source, so the app can treat them interchangeably
TrackSignals = {
    "connected": (GObject.SignalFlags.RUN_FIRST, None, ()),
    "disconnected": (GObject.SignalFlags.RUN_FIRST, None, ()),
    "track-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    "state-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    "seeked": (GObject.SignalFlags.RUN_FIRST, None, (GObject.TYPE_INT64,)),
}


@dataclass(frozen=True)
class Track:
    title: str = ""
    artist: str = ""
    album: str = ""
    length_ms: int = 0
    url: str = ""

    @classmethod
    def from_metadata(cls, metadata: dict | None) -> "Track":
        if not metadata:
            return cls()
        artists = metadata.get("xesam:artist") or []
        if isinstance(artists, str):
            artists = [artists]
        return cls(
            title=str(metadata.get("xesam:title") or ""),
            artist=" / ".join(str(a) for a in artists if a),
            album=str(metadata.get("xesam:album") or ""),
            length_ms=int(metadata.get("mpris:length") or 0) // 1000,
            url=str(metadata.get("xesam:url") or ""),
        )

    def same_song(self, other: "Track | None") -> bool:
        """Whether this is the same song, ignoring the player's ids and length.

        A player may reissue ``mpris:trackid`` for the same file, or re-emit
        Metadata with ``mpris:length`` momentarily zeroed; neither should trigger
        a second lyrics lookup.
        """
        if other is None or not self.title or not other.title:
            return False
        return (self.title, self.artist, self.album) == (
            other.title,
            other.artist,
            other.album,
        )


class MprisClient(GObject.Object):
    __gsignals__ = TrackSignals

    def __init__(self, bus_name: str) -> None:
        super().__init__()
        self.bus_name = bus_name
        self.key = bus_name
        self.label = _short_name(bus_name)
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.proxy: Gio.DBusProxy | None = None
        self.track = Track()
        self.status = "Stopped"
        self._watch_id = 0

    def start(self) -> None:
        """Begin watching for the player.

        Kept separate from __init__ because the appeared callback fires
        synchronously when the player is already running -- the owner has to
        connect its signals before that happens.
        """
        self._watch_id = Gio.bus_watch_name_on_connection(
            self.bus,
            self.bus_name,
            Gio.BusNameWatcherFlags.NONE,
            self._on_appeared,
            self._on_vanished,
        )

    @property
    def connected(self) -> bool:
        return self.proxy is not None

    def _on_appeared(self, *_args) -> None:
        try:
            self.proxy = Gio.DBusProxy.new_for_bus_sync(
                Gio.BusType.SESSION,
                Gio.DBusProxyFlags.NONE,
                None,
                self.bus_name,
                OBJECT_PATH,
                PLAYER_IFACE,
                None,
            )
        except GLib.Error:
            self.proxy = None
            return
        self.proxy.connect("g-properties-changed", self._on_properties_changed)
        self.proxy.connect("g-signal", self._on_signal)
        self._read_identity()
        self._update_track()
        self._update_status()
        self.emit("connected")
        self.emit("track-changed")

    def _read_identity(self) -> None:
        """The player's own display name, for the player picker."""
        try:
            app = Gio.DBusProxy.new_for_bus_sync(
                Gio.BusType.SESSION, Gio.DBusProxyFlags.NONE, None,
                self.bus_name, OBJECT_PATH, APP_IFACE, None,
            )
            identity = app.get_cached_property("Identity")
            if identity is not None and str(identity.unpack()).strip():
                self.label = str(identity.unpack()).strip()
        except GLib.Error:
            pass

    def lyric_text(self) -> str | None:
        return None

    def _on_vanished(self, *_args) -> None:
        self.proxy = None
        self.track = Track()
        self.status = "Stopped"
        self.emit("disconnected")

    def _on_properties_changed(self, _proxy, changed, invalidated) -> None:
        keys = set(changed.unpack().keys()) | set(invalidated or [])
        if "Metadata" in keys:
            self._update_track()
            self.emit("track-changed")
        if "PlaybackStatus" in keys:
            self._update_status()
            self.emit("state-changed")

    def _on_signal(self, _proxy, _sender, signal_name, params) -> None:
        if signal_name == "Seeked":
            self.emit("seeked", int(params.unpack()[0]))

    def _update_track(self) -> None:
        value = self.proxy.get_cached_property("Metadata") if self.proxy else None
        self.track = Track.from_metadata(value.unpack() if value is not None else None)

    def _update_status(self) -> None:
        value = self.proxy.get_cached_property("PlaybackStatus") if self.proxy else None
        self.status = str(value.unpack()) if value is not None else "Stopped"

    def get_property(self, name: str):
        """Read a property live -- the proxy's cached copy of Position is stale."""
        result = self.proxy.call_sync(
            f"{PROPS_IFACE}.Get",
            GLib.Variant("(ss)", (PLAYER_IFACE, name)),
            Gio.DBusCallFlags.NONE,
            CALL_TIMEOUT_MS,
            None,
        )
        return result.unpack()[0]

    def position_us(self) -> int | None:
        try:
            return int(self.get_property("Position"))
        except GLib.Error:
            return None

    def rate(self) -> float:
        try:
            return float(self.get_property("Rate"))
        except GLib.Error:
            return 1.0

    def can_control(self) -> bool:
        """Whether the player says it takes play/pause and skip at all."""
        value = self.proxy.get_cached_property("CanControl") if self.proxy else None
        return bool(value.unpack()) if value is not None else False

    def control(self, action: str) -> bool:
        """Ask the player to play, pause or skip; False where it will not.

        Called on the main thread from a click. Each of these is a local D-Bus
        call to a process that is already running, so it costs less than the
        redraw that follows it.
        """
        method = CONTROL_METHODS.get(action)
        if self.proxy is None or method is None:
            return False
        try:
            self.proxy.call_sync(
                method, None, Gio.DBusCallFlags.NONE, CALL_TIMEOUT_MS, None
            )
        except GLib.Error as exc:
            print(f"PlanaLyrics: {self.label} 拒绝 {method} ({exc.message})")
            return False
        return True

    def player_pid(self) -> int | None:
        """PID of the player process, for resolving its open audio files."""
        try:
            result = self.bus.call_sync(
                "org.freedesktop.DBus",
                "/org/freedesktop/DBus",
                "org.freedesktop.DBus",
                "GetConnectionUnixProcessID",
                GLib.Variant("(s)", (self.bus_name,)),
                None,
                Gio.DBusCallFlags.NONE,
                CALL_TIMEOUT_MS,
                None,
            )
        except GLib.Error:
            return None
        return int(result.unpack()[0])


def _short_name(bus_name: str) -> str:
    """A readable fallback for a player with no Identity, e.g. chrome.instance12."""
    tail = bus_name.rsplit(".", 1)[-1]
    for suffix in ("instance", "player"):
        if suffix in tail:
            tail = tail.split(suffix)[0] or tail
    if len(tail) > 3 and tail[-3:].isdigit():
        tail = tail[:-3]
    return (tail or bus_name).strip().replace("_", " ").title() or bus_name
