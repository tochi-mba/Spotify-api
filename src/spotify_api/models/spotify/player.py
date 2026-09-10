"""Player objects: devices, playback state and the queue."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import Field

from spotify_api.models.spotify.common import ExternalUrls, SpotifyModel

__all__ = [
    "Context",
    "CurrentlyPlaying",
    "Device",
    "Devices",
    "PlayHistory",
    "PlaybackState",
    "Queue",
    "RepeatState",
]


class RepeatState(StrEnum):
    """What Spotify does when the current thing ends."""

    OFF = "off"
    TRACK = "track"
    CONTEXT = "context"


class Device(SpotifyModel):
    """Somewhere audio can play."""

    id: str | None = Field(default=None, description="Device ID. Null for restricted devices.")
    is_active: bool = Field(default=False, description="Whether this device is playing now.")
    is_private_session: bool = Field(default=False, description="Private session in progress.")
    is_restricted: bool = Field(
        default=False, description="If true, this device will not accept Web API commands."
    )
    name: str = Field(default="", description="Human-readable device name.")
    type: str = Field(default="", description="Computer, Smartphone, Speaker, ...")
    volume_percent: int | None = Field(default=None, description="Volume, 0-100.")
    supports_volume: bool = Field(
        default=False, description="Whether volume can be set on this device."
    )


class Devices(SpotifyModel):
    """The devices available to this account."""

    devices: list[Device] = Field(default_factory=list, description="Known devices.")


class Context(SpotifyModel):
    """What is being played from -- an album, playlist or artist."""

    type: str | None = Field(default=None, description="album, artist, playlist or show.")
    href: str | None = Field(default=None, description="API URL of the context.")
    external_urls: ExternalUrls | None = Field(default=None, description="Links to the context.")
    uri: str | None = Field(default=None, description="Spotify URI of the context.")


class PlaybackState(SpotifyModel):
    """What is playing, where, and how.

    This is what the async confirmation polls: it is the only authority on
    whether a command actually took effect.
    """

    device: Device | None = Field(default=None, description="The device in use.")
    repeat_state: RepeatState | None = Field(default=None, description="off, track or context.")
    shuffle_state: bool = Field(default=False, description="Whether shuffle is on.")
    context: Context | None = Field(default=None, description="What is being played from.")
    timestamp: int | None = Field(default=None, description="When the state was fetched, ms.")
    progress_ms: int | None = Field(default=None, description="Position in the current item.")
    is_playing: bool = Field(default=False, description="Whether audio is actually playing.")
    item: dict[str, Any] | None = Field(
        default=None, description="The track or episode currently loaded."
    )
    currently_playing_type: str | None = Field(
        default=None, description="track, episode, ad or unknown."
    )
    actions: dict[str, Any] | None = Field(
        default=None, description="Which controls are permitted right now."
    )


class CurrentlyPlaying(PlaybackState):
    """The narrower answer from /me/player/currently-playing."""


class PlayHistory(SpotifyModel):
    """One entry from the recently-played list."""

    track: dict[str, Any] | None = Field(default=None, description="The track that played.")
    played_at: str | None = Field(default=None, description="When it played, ISO 8601.")
    context: Context | None = Field(default=None, description="What it played from.")


class RecentlyPlayed(SpotifyModel):
    """A cursor-paged page of listening history."""

    href: str | None = Field(default=None, description="URL of this page.")
    limit: int = Field(default=20, description="Page size that was applied.")
    next: str | None = Field(default=None, description="URL of the next page, if any.")
    cursors: dict[str, Any] | None = Field(default=None, description="Markers for paging.")
    items: list[PlayHistory] = Field(default_factory=list, description="Plays, newest first.")


class Queue(SpotifyModel):
    """What is playing and what comes next."""

    currently_playing: dict[str, Any] | None = Field(
        default=None, description="The item playing now."
    )
    queue: list[dict[str, Any]] = Field(default_factory=list, description="Items queued behind it.")
