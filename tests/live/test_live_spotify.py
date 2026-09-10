"""Smoke tests against the real Spotify Web API.

Deselected by default (``-m "not live"`` in pyproject) and skipped outright
when credentials are absent, so a contributor without a Spotify application can
still run the full suite. Run them explicitly with::

    uv run pytest -m live

These are deliberately few and loose. They answer one question -- "do our
assumptions still hold against the real thing?" -- and must not assert on
values Spotify is free to change, such as popularity or preview URLs.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import httpx
import pytest

from spotify_api.app import create_app
from spotify_api.config import Settings
from spotify_api.models.requests import LookupItem
from spotify_api.models.responses import LookupStatus
from spotify_api.spotify.auth import ClientCredentialsProvider
from spotify_api.spotify.client import SpotifyClient
from spotify_api.spotify.resolver import SpotifyTrackResolver

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not (os.getenv("SPOTIFY_CLIENT_ID") and os.getenv("SPOTIFY_CLIENT_SECRET")),
        reason="SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET are not set",
    ),
]


@pytest.fixture
def settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


@pytest.fixture
async def resolver(settings: Settings) -> AsyncIterator[SpotifyTrackResolver]:
    async with httpx.AsyncClient() as client:
        provider = ClientCredentialsProvider(client=client, settings=settings)
        spotify = SpotifyClient(client=client, token_provider=provider, settings=settings)
        yield SpotifyTrackResolver(client=spotify, settings=settings)


async def test_credentials_are_accepted_and_a_token_is_issued(settings: Settings) -> None:
    async with httpx.AsyncClient() as client:
        provider = ClientCredentialsProvider(client=client, settings=settings)
        assert await provider.get_token()


async def test_a_well_known_track_resolves(resolver: SpotifyTrackResolver) -> None:
    [result] = await resolver.resolve(
        [LookupItem(name="Bohemian Rhapsody", artist="Queen", year=1975)]
    )

    assert result.status is LookupStatus.FOUND
    assert result.track is not None
    assert "bohemian rhapsody" in result.track.name.lower()
    assert any("queen" in artist.name.lower() for artist in result.track.artists)
    assert result.track.uri.startswith("spotify:track:")


async def test_a_nonsense_query_is_not_found(resolver: SpotifyTrackResolver) -> None:
    [result] = await resolver.resolve(
        [LookupItem(name="zzzz nonexistent track qqqq", artist="zzzz nobody qqqq")]
    )
    assert result.status is LookupStatus.NOT_FOUND


async def test_a_mixed_batch_returns_one_result_per_item(
    resolver: SpotifyTrackResolver,
) -> None:
    items = [
        LookupItem(name="Redbone", artist="Childish Gambino"),
        LookupItem(name="zzzz nonexistent qqqq"),
        LookupItem(name="Take Five", artist="The Dave Brubeck Quartet"),
    ]
    results = await resolver.resolve(items)

    assert [r.index for r in results] == [0, 1, 2]
    assert results[0].status is LookupStatus.FOUND
    assert results[1].status is LookupStatus.NOT_FOUND
    assert results[2].status is LookupStatus.FOUND


async def test_the_service_reports_itself_ready(settings: Settings) -> None:
    app = create_app(settings=settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/ready")
    assert response.status_code == 200
