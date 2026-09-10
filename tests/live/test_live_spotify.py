"""Smoke tests against a real keyring and the real Spotify Web API.

Deselected by default (``-m "not live"`` in pyproject) and skipped outright
unless the environment names a running keyring and supplies a user token, so a
contributor without either can still run the full suite::

    export KEYRING_BASE_URL=http://127.0.0.1:8001
    export KEYRING_SERVICE_TOKEN=...      # this service's token, from keyring
    export SPOTIFY_LIVE_USER_TOKEN=...    # mint with: POST /v1/auth/service-token
    uv run pytest -m live

These are deliberately few and loose. They answer one question -- "do our
assumptions still hold against the real thing?" -- and must not assert on
values Spotify is free to change, such as popularity or preview URLs.

Nothing here starts playback. Tests that would take over a speaker are gated
separately on ``SPOTIFY_LIVE_DEVICE_ID``, so running the live suite can never
interrupt whatever you happen to be listening to.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import httpx
import pytest
from pydantic import SecretStr

from spotify_api.app import create_app
from spotify_api.config import Settings
from spotify_api.credentials.keyring import KeyringCredentialProvider
from spotify_api.credentials.models import UserContext
from spotify_api.models.requests import LookupItem
from spotify_api.models.responses import LookupStatus
from spotify_api.spotify.client import SpotifyClient
from spotify_api.spotify.resolver import SpotifyTrackResolver

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

USER_TOKEN = os.getenv("SPOTIFY_LIVE_USER_TOKEN", "")

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not (os.getenv("KEYRING_BASE_URL") and os.getenv("KEYRING_SERVICE_TOKEN") and USER_TOKEN),
        reason="KEYRING_BASE_URL, KEYRING_SERVICE_TOKEN and SPOTIFY_LIVE_USER_TOKEN are not set",
    ),
]


@pytest.fixture
def settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


@pytest.fixture
def context(settings: Settings) -> UserContext:
    return UserContext(user_token=SecretStr(USER_TOKEN), profile=settings.keyring_default_profile)


@pytest.fixture
async def resolver(settings: Settings) -> AsyncIterator[SpotifyTrackResolver]:
    async with httpx.AsyncClient() as client:
        credentials = KeyringCredentialProvider(client=client, settings=settings)
        spotify = SpotifyClient(client=client, credentials=credentials, settings=settings)
        yield SpotifyTrackResolver(client=spotify, settings=settings)


async def test_keyring_hands_over_a_usable_spotify_credential(
    settings: Settings, context: UserContext
) -> None:
    async with httpx.AsyncClient() as client:
        credentials = KeyringCredentialProvider(client=client, settings=settings)
        resolved = await credentials.resolve(
            user_token=context.user_token.get_secret_value(), profile=context.profile
        )
    assert "Authorization" in resolved.headers


async def test_a_well_known_track_resolves(
    resolver: SpotifyTrackResolver, context: UserContext
) -> None:
    [result] = await resolver.resolve(
        [LookupItem(name="Bohemian Rhapsody", artist="Queen", year=1975)], context=context
    )

    assert result.status is LookupStatus.FOUND
    assert result.track is not None
    assert "bohemian rhapsody" in result.track.name.lower()
    assert any("queen" in artist.name.lower() for artist in result.track.artists)
    assert result.track.uri.startswith("spotify:track:")


async def test_a_nonsense_query_is_not_found(
    resolver: SpotifyTrackResolver, context: UserContext
) -> None:
    [result] = await resolver.resolve(
        [LookupItem(name="zzzz nonexistent track qqqq", artist="zzzz nobody qqqq")],
        context=context,
    )
    assert result.status is LookupStatus.NOT_FOUND


async def test_a_mixed_batch_returns_one_result_per_item(
    resolver: SpotifyTrackResolver, context: UserContext
) -> None:
    items = [
        LookupItem(name="Redbone", artist="Childish Gambino"),
        LookupItem(name="zzzz nonexistent qqqq"),
        LookupItem(name="Take Five", artist="The Dave Brubeck Quartet"),
    ]
    results = await resolver.resolve(items, context=context)

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


async def test_lookup_end_to_end_through_the_http_layer(settings: Settings) -> None:
    app = create_app(settings=settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/v1/lookup",
                json={"items": [{"name": "Bohemian Rhapsody", "artist": "Queen"}]},
                headers={"X-Keyring-User-Token": USER_TOKEN},
            )

    assert response.status_code == 200
    assert response.json()["results"][0]["status"] == "found"
