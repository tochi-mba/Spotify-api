"""The whole stack, with the network as the only thing faked.

Every other integration test injects a fake resolver to isolate the HTTP
contract. This one goes through the real graph -- route, user context, keyring
credential provider, Spotify client, mappers -- against an httpx MockTransport
serving both upstreams, so the wiring production actually uses is proven rather
than assumed.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from spotify_api.app import create_app
from tests.factories import make_settings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

KEYRING_URL = "https://keyring.test"
SPOTIFY_URL = "https://api.spotify.test/v1"
USER_TOKEN = "e2e-user-token"


class FakeUpstreams:
    """Stands in for both keyring and Spotify, recording what each was asked."""

    def __init__(self, search_found: dict[str, Any], search_empty: dict[str, Any]) -> None:
        self.search_found = search_found
        self.search_empty = search_empty
        self.keyring_calls: list[httpx.Request] = []
        self.spotify_calls: list[httpx.Request] = []
        self.keyring_status = 200
        self.spotify_status = 200
        self.access_token = "spotify-access-token"

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "keyring.test":
            return self._keyring(request)
        return self._spotify(request)

    def _keyring(self, request: httpx.Request) -> httpx.Response:
        self.keyring_calls.append(request)
        if self.keyring_status != 200:
            return httpx.Response(self.keyring_status, json={"detail": "no"})
        if request.url.path.endswith("/healthz"):
            return httpx.Response(200, json={"status": "ok"})
        expires = dt.datetime.now(tz=dt.UTC) + dt.timedelta(hours=1)
        return httpx.Response(
            200,
            json={
                "service": "spotify",
                "headers": {"Authorization": f"Bearer {self.access_token}"},
                "query_params": {},
                "expires_at": expires.isoformat(),
            },
        )

    def _spotify(self, request: httpx.Request) -> httpx.Response:
        self.spotify_calls.append(request)
        if self.spotify_status != 200:
            return httpx.Response(self.spotify_status, json={"error": {"message": "nope"}})
        query = request.url.params.get("q", "")
        body = self.search_found if "Bohemian" in query else self.search_empty
        return httpx.Response(200, json=body)


@pytest.fixture
def upstreams(search_found: dict[str, Any], search_empty: dict[str, Any]) -> FakeUpstreams:
    return FakeUpstreams(search_found, search_empty)


@pytest.fixture
async def live_client(upstreams: FakeUpstreams) -> AsyncIterator[httpx.AsyncClient]:
    settings = make_settings(keyring_base_url=KEYRING_URL, spotify_api_base_url=SPOTIFY_URL)
    app = create_app(settings=settings, transport=httpx.MockTransport(upstreams))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"X-Keyring-User-Token": USER_TOKEN},
        ) as client:
            yield client


async def test_a_batch_flows_through_the_real_graph(
    live_client: httpx.AsyncClient, upstreams: FakeUpstreams
) -> None:
    response = await live_client.post(
        "/v1/lookup",
        json={
            "items": [
                {"name": "Bohemian Rhapsody", "artist": "Queen", "year": 1975},
                {"name": "Definitely Not A Real Track"},
            ],
            "market": "GB",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 2

    first, second = payload["results"]
    assert first["status"] == "found"
    assert first["track"]["name"] == "Bohemian Rhapsody"
    assert first["track"]["isrc"] == "GBUM71029604"
    assert first["track"]["album"]["release_year"] == 1975
    assert second["status"] == "not_found"

    searches = upstreams.spotify_calls
    assert len(searches) == 2
    assert searches[0].url.params["market"] == "GB"
    assert searches[0].url.params["q"] == 'track:"Bohemian Rhapsody" artist:"Queen" year:1975'


async def test_keyring_is_asked_for_the_credential_and_spotify_gets_it(
    live_client: httpx.AsyncClient, upstreams: FakeUpstreams
) -> None:
    await live_client.post("/v1/lookup", json={"items": [{"name": "Bohemian Rhapsody"}]})

    ask = upstreams.keyring_calls[0]
    assert ask.url.path == "/v1/internal/credentials/personal/spotify"
    assert ask.headers["Authorization"] == "Bearer test-service-token"
    assert ask.headers["X-Keyring-User-Token"] == USER_TOKEN

    # And what keyring returned is what reached Spotify.
    assert upstreams.spotify_calls[0].headers["Authorization"] == "Bearer spotify-access-token"


async def test_the_credential_is_resolved_once_for_a_whole_batch(
    live_client: httpx.AsyncClient, upstreams: FakeUpstreams
) -> None:
    # Ten items must not mean ten keyring round-trips.
    await live_client.post(
        "/v1/lookup", json={"items": [{"name": f"track {n}"} for n in range(10)]}
    )
    assert len(upstreams.keyring_calls) == 1
    assert len(upstreams.spotify_calls) == 10


async def test_the_profile_header_selects_the_keyring_profile(
    live_client: httpx.AsyncClient, upstreams: FakeUpstreams
) -> None:
    await live_client.post(
        "/v1/lookup",
        json={"items": [{"name": "x"}]},
        headers={"X-Keyring-Profile": "work"},
    )
    assert upstreams.keyring_calls[0].url.path == "/v1/internal/credentials/work/spotify"


async def test_readiness_uses_the_real_credential_provider(
    live_client: httpx.AsyncClient, upstreams: FakeUpstreams
) -> None:
    response = await live_client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "dependencies": {"spotify": "ok"}}
    assert upstreams.keyring_calls


async def test_readiness_reports_not_ready_when_keyring_is_down(
    live_client: httpx.AsyncClient, upstreams: FakeUpstreams
) -> None:
    upstreams.keyring_status = 503
    response = await live_client.get("/ready")
    assert response.status_code == 503
    assert response.json()["dependencies"]["spotify"] == "unavailable"


async def test_a_refused_user_token_surfaces_as_401(
    live_client: httpx.AsyncClient, upstreams: FakeUpstreams
) -> None:
    upstreams.keyring_status = 401
    response = await live_client.post("/v1/lookup", json={"items": [{"name": "x"}]})

    assert response.status_code == 401
    assert response.json()["error"]["type"] == "user_token_rejected"


async def test_a_missing_spotify_connection_surfaces_distinctly(
    live_client: httpx.AsyncClient, upstreams: FakeUpstreams
) -> None:
    upstreams.keyring_status = 404
    response = await live_client.post("/v1/lookup", json={"items": [{"name": "x"}]})

    assert response.status_code == 502
    assert response.json()["error"]["type"] == "credential_unavailable"


async def test_an_upstream_spotify_failure_surfaces_as_a_per_item_error(
    live_client: httpx.AsyncClient, upstreams: FakeUpstreams
) -> None:
    upstreams.spotify_status = 500
    response = await live_client.post("/v1/lookup", json={"items": [{"name": "Bohemian Rhapsody"}]})

    assert response.status_code == 200
    [result] = response.json()["results"]
    assert result["status"] == "error"
    assert "failing" in result["error"]
