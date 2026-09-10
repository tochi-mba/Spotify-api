"""Outbound response models.

The lookup endpoint answers with *partial success*: the batch always returns
one result per submitted item, each carrying its own status. A single track
that cannot be resolved never costs the caller the other forty-nine.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, computed_field

from spotify_api import SERVICE_NAME
from spotify_api.models.requests import LookupItem

__all__ = [
    "Album",
    "Artist",
    "ErrorBody",
    "ErrorResponse",
    "HealthResponse",
    "LookupResponse",
    "LookupResult",
    "LookupStatus",
    "ReadyResponse",
    "Track",
]


class LookupStatus(StrEnum):
    """Outcome of resolving a single item."""

    FOUND = "found"
    NOT_FOUND = "not_found"
    ERROR = "error"


class Artist(BaseModel):
    """A performing artist credited on a track."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(description="Spotify artist ID.")
    name: str = Field(description="Artist display name.")


class Album(BaseModel):
    """The album a track appears on."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(description="Spotify album ID.")
    name: str = Field(description="Album title.")
    release_date: str | None = Field(
        default=None,
        description="Release date as Spotify reports it: YYYY, YYYY-MM or YYYY-MM-DD.",
    )
    release_year: int | None = Field(
        default=None,
        description="Year parsed from release_date, or null when it is unparseable.",
    )


class Track(BaseModel):
    """A resolved Spotify track."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(description="Spotify track ID.")
    name: str = Field(description="Track title as catalogued by Spotify.")
    artists: list[Artist] = Field(description="Credited artists, in Spotify's order.")
    album: Album = Field(description="Album the track appears on.")
    duration_ms: int = Field(description="Track length in milliseconds.")
    explicit: bool = Field(description="Whether Spotify flags the track as explicit.")
    popularity: int | None = Field(default=None, description="Spotify popularity score, 0-100.")
    isrc: str | None = Field(
        default=None, description="International Standard Recording Code, when published."
    )
    preview_url: str | None = Field(
        default=None, description="30-second MP3 preview, when one is available."
    )
    external_url: str | None = Field(default=None, description="open.spotify.com page.")
    uri: str = Field(description="Spotify URI, e.g. spotify:track:...")


class LookupResult(BaseModel):
    """The outcome for one submitted item."""

    index: int = Field(description="Zero-based position of this item in the request.")
    query: LookupItem = Field(description="The item as it was received, after validation.")
    status: LookupStatus = Field(description="found, not_found or error.")
    track: Track | None = Field(default=None, description="Populated only when status is found.")
    error: str | None = Field(
        default=None, description="Human-readable reason, populated only when status is error."
    )


class LookupResponse(BaseModel):
    """The answer to a batch lookup."""

    request_id: str = Field(description="Correlation id, also returned as the X-Request-ID header.")
    results: list[LookupResult] = Field(
        description="One entry per submitted item, in the order they were submitted."
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def count(self) -> int:
        """Number of results, always equal to the number of submitted items."""
        return len(self.results)


class HealthResponse(BaseModel):
    """Liveness answer. Deliberately free of any external dependency."""

    status: Annotated[str, Field(description="Always 'ok' while the process is serving.")] = "ok"
    service: Annotated[str, Field(description="Stable service identifier.")] = SERVICE_NAME
    version: str = Field(description="Running version of the service.")
    uptime_seconds: float = Field(description="Seconds since the application started.")


class ReadyResponse(BaseModel):
    """Readiness answer, reflecting whether upstreams are usable."""

    status: str = Field(description="'ready' or 'not_ready'.")
    dependencies: dict[str, str] = Field(description="Per-dependency health.")

    @classmethod
    def from_spotify_health(cls, *, spotify_ok: bool) -> Self:
        """Build a readiness answer from the state of the Spotify dependency."""
        return cls(
            status="ready" if spotify_ok else "not_ready",
            dependencies={"spotify": "ok" if spotify_ok else "unavailable"},
        )


class ErrorBody(BaseModel):
    """The inner object of the error envelope."""

    type: str = Field(description="Stable, machine-readable error discriminator.")
    message: str = Field(description="Human-readable explanation.")
    details: dict[str, object] = Field(
        default_factory=dict, description="Structured context, when there is any."
    )
    request_id: str = Field(description="Correlation id for this failure.")


class ErrorResponse(BaseModel):
    """Every non-2xx response this service produces has this shape."""

    error: ErrorBody
