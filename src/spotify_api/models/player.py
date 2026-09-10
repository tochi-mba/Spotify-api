"""Request bodies for the Player endpoints.

Spotify's own player API mixes query parameters and JSON bodies in ways that
are easy to get wrong. These models put every option in the body, validated,
so a caller has one place to look and one shape to send.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from spotify_api.models.spotify.player import RepeatState

__all__ = [
    "PlayRequest",
    "QueueRequest",
    "RepeatRequest",
    "SeekRequest",
    "ShuffleRequest",
    "TransferRequest",
    "VolumeRequest",
]

#: Spotify's ceiling on how much can be queued in one play call.
MAX_URIS = 750


class _PlayerRequest(BaseModel):
    """Shared shape: every player command may name a device."""

    model_config = ConfigDict(extra="forbid")

    device_id: str | None = Field(
        default=None,
        description=(
            "Device to act on. Omit to use whatever is active. If nothing is active "
            "the call fails with no_active_device -- list /v1/player/devices to pick one."
        ),
    )


class PlayRequest(_PlayerRequest):
    """Start or resume playback."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {"uris": ["spotify:track:7tFiyTwD0nx5a1eklYtX2J"]},
                {"context_uri": "spotify:album:1GbtB4zTqAsyfZEsm1RZfx", "offset": {"position": 3}},
                {},
            ]
        },
    )

    context_uri: str | None = Field(
        default=None, description="Album, artist or playlist to play from."
    )
    uris: Annotated[list[str], Field(max_length=MAX_URIS)] | None = Field(
        default=None, description="Specific track URIs to play."
    )
    offset: dict[str, Any] | None = Field(
        default=None,
        description='Where to start within context_uri: {"position": 3} or {"uri": "..."}.',
    )
    position_ms: int | None = Field(
        default=None, ge=0, description="Start this far into the first item."
    )

    @model_validator(mode="after")
    def _not_both_sources(self) -> PlayRequest:
        """Spotify accepts one source or the other, never both."""
        if self.context_uri is not None and self.uris is not None:
            message = "give either context_uri or uris, not both"
            raise ValueError(message)
        return self


class SeekRequest(_PlayerRequest):
    """Jump to a position in the current item."""

    position_ms: int = Field(ge=0, description="Milliseconds from the start of the item.")


class VolumeRequest(_PlayerRequest):
    """Set the device volume."""

    volume_percent: int = Field(ge=0, le=100, description="Volume, 0-100.")


class ShuffleRequest(_PlayerRequest):
    """Turn shuffle on or off."""

    state: bool = Field(description="True to shuffle.")


class RepeatRequest(_PlayerRequest):
    """Set the repeat mode."""

    state: RepeatState = Field(description="off, track or context.")


class TransferRequest(BaseModel):
    """Move playback to another device."""

    model_config = ConfigDict(extra="forbid")

    device_id: str = Field(description="Device to move playback to.")
    play: bool | None = Field(
        default=None,
        description="True to start playing on arrival; omit to keep the current state.",
    )


class QueueRequest(_PlayerRequest):
    """Append an item to the queue."""

    uri: str = Field(min_length=1, description="Track or episode URI to queue.")
