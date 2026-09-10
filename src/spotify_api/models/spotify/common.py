"""Objects that appear inside many Spotify responses."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["ExternalIds", "ExternalUrls", "Followers", "Image", "Restrictions", "SpotifyModel"]


class SpotifyModel(BaseModel):
    """Base for everything Spotify sends us.

    ``extra="ignore"`` is deliberate and load-bearing: Spotify adds fields to
    its responses without notice, and rejecting them would turn a working
    endpoint into a 500 the day they do.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class ExternalUrls(SpotifyModel):
    """Links to the object on Spotify's own site."""

    spotify: str | None = Field(default=None, description="open.spotify.com page.")


class ExternalIds(SpotifyModel):
    """Third-party identifiers for a recording or release."""

    isrc: str | None = Field(default=None, description="International Standard Recording Code.")
    ean: str | None = Field(default=None, description="International Article Number.")
    upc: str | None = Field(default=None, description="Universal Product Code.")


class Image(SpotifyModel):
    """Cover art at one size."""

    url: str = Field(description="Image URL. Expires -- do not cache indefinitely.")
    height: int | None = Field(default=None, description="Pixels, when Spotify knows.")
    width: int | None = Field(default=None, description="Pixels, when Spotify knows.")


class Followers(SpotifyModel):
    """How many people follow something."""

    href: str | None = Field(default=None, description="Always null; Spotify reserves it.")
    total: int = Field(default=0, description="Follower count.")


class Restrictions(SpotifyModel):
    """Why content is unavailable, when it is."""

    reason: str | None = Field(default=None, description="market, product or explicit.")


class Paging(SpotifyModel):
    """Spotify's standard offset-paged envelope."""

    href: str | None = Field(default=None, description="URL of this page.")
    limit: int = Field(default=20, description="Page size that was applied.")
    offset: int = Field(default=0, description="Index of the first item.")
    total: int = Field(default=0, description="Items available in total.")
    next: str | None = Field(default=None, description="URL of the next page, if any.")
    previous: str | None = Field(default=None, description="URL of the previous page, if any.")
    items: list[Any] = Field(default_factory=list, description="This page's items.")


class CursorPaging(SpotifyModel):
    """Spotify's cursor-paged envelope, used where offsets make no sense."""

    href: str | None = Field(default=None, description="URL of this page.")
    limit: int = Field(default=20, description="Page size that was applied.")
    total: int | None = Field(default=None, description="Items available, where known.")
    next: str | None = Field(default=None, description="URL of the next page, if any.")
    cursors: dict[str, Any] | None = Field(
        default=None, description="Opaque markers for the next page."
    )
    items: list[Any] = Field(default_factory=list, description="This page's items.")
