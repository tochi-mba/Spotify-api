"""Objects that appear inside many Spotify responses."""

from __future__ import annotations

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
