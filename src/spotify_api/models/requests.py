"""Inbound request models.

These types *are* the API contract. Every rule a caller must satisfy is encoded
here rather than in a route handler, so the contract is enforced in exactly one
place and is published automatically in the OpenAPI schema.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "MAX_BATCH_ITEMS",
    "MAX_TEXT_LENGTH",
    "MIN_YEAR",
    "LookupItem",
    "LookupRequest",
]

#: Longest free-text value accepted for a name, artist or album.
MAX_TEXT_LENGTH = 200

#: Recorded music predates this, but Spotify's catalogue effectively does not.
MIN_YEAR = 1900

#: Hard ceiling on batch size. The *operational* limit is ``max_batch_size`` in
#: settings; this is the absolute bound the schema itself will accept.
MAX_BATCH_ITEMS = 200

#: Length of an ISO 3166-1 alpha-2 country code.
MARKET_CODE_LENGTH = 2

_Text = Annotated[str, Field(max_length=MAX_TEXT_LENGTH)]


def _normalise_market(value: str | None) -> str | None:
    """Upper-case a market code, treating blank as absent."""
    if value is None:
        return None
    normalised = value.strip().upper()
    return normalised or None


def _validate_market(value: str | None) -> str | None:
    """Reject anything that is not a two-letter country code."""
    if value is not None and not (len(value) == MARKET_CODE_LENGTH and value.isalpha()):
        message = "market must be an ISO 3166-1 alpha-2 country code, e.g. 'GB'"
        raise ValueError(message)
    return value


class LookupItem(BaseModel):
    """One track to resolve.

    Only ``name`` is required. The optional fields are *hints*: each one that is
    supplied narrows the Spotify search, which materially improves accuracy for
    common titles.
    """

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        json_schema_extra={
            "examples": [
                {
                    "name": "Bohemian Rhapsody",
                    "artist": "Queen",
                    "album": "A Night at the Opera",
                    "year": 1975,
                }
            ]
        },
    )

    name: Annotated[str, Field(min_length=1, max_length=MAX_TEXT_LENGTH)] = Field(
        description="Track title. Required.",
    )
    artist: _Text | None = Field(
        default=None,
        description="Performing artist. Optional, but strongly recommended.",
    )
    album: _Text | None = Field(
        default=None,
        description="Album the track appears on. Optional.",
    )
    year: int | None = Field(
        default=None,
        description="Release year. Optional.",
    )

    @field_validator("artist", "album", mode="after")
    @classmethod
    def _blank_is_absent(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value or None

    @field_validator("year")
    @classmethod
    def _year_is_plausible(cls, value: int | None) -> int | None:
        if value is None:
            return None
        ceiling = dt.datetime.now(tz=dt.UTC).year + 1
        if not MIN_YEAR <= value <= ceiling:
            message = f"year must be between {MIN_YEAR} and {ceiling}"
            raise ValueError(message)
        return value


class LookupRequest(BaseModel):
    """A batch of tracks to resolve in one call."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "items": [
                        {"name": "Bohemian Rhapsody", "artist": "Queen", "year": 1975},
                        {"name": "Redbone"},
                    ],
                    "market": "GB",
                }
            ]
        },
    )

    items: Annotated[list[LookupItem], Field(min_length=1, max_length=MAX_BATCH_ITEMS)] = Field(
        description="Tracks to resolve. Results are returned in the same order.",
    )
    market: str | None = Field(
        default=None,
        description=(
            "ISO 3166-1 alpha-2 market. Affects track availability and which "
            "release of a track is returned. Falls back to the service default."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _normalise_market_field(cls, data: Any) -> Any:  # noqa: ANN401
        if isinstance(data, dict) and "market" in data:
            data = {**data, "market": _normalise_market(data["market"])}
        return data

    @field_validator("market")
    @classmethod
    def _market_is_a_country_code(cls, value: str | None) -> str | None:
        return _validate_market(value)
