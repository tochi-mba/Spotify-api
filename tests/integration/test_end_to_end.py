"""The whole stack, with the network as the only thing faked.

Every other integration test injects a fake resolver to isolate the HTTP
contract. This one goes through the real graph -- route, resolver, client,
token provider, mappers -- against an httpx MockTransport, so the wiring that
production actually uses is proven rather than assumed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
import pytest

from spotify_api.app import create_app
from tests.factories import make_settings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

TOKEN_PATH = "/api/token"
SEARCH_PATH = "/search"


@pytest.fixture
def spotify(search_found: dict[str, Any], search_empty: dict[str, Any]) -> Any:
    """A stand-in Spotify, recording what it was asked."""

    class FakeSpotify:
        def __init__(self) -> None:
            self.requests: list[httpx.Request] = []
            self.token_calls = 0
            self.search_status = 200

        def __call__(self, request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if request.url.path.endswith(TOKEN_PATH):
                self.token_calls += 1
                return httpx.Response(
                    200,
                    json={
                        "access_token": "live-token",
                        "token_type": "Bearer",
                        "expires_in": 3600,
                    },
                )
            if self.search_status != 200:
                return httpx.Response(self.search_status, json={"error": "nope"})
            query = request.url.params.get("q", "")
            body = search_found if "Bohemian" in query else search_empty
            return httpx.Response(200, json=body)

    return FakeSpotify()


@pytest.fixture
async def live_client(spotify: Any) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(settings=make_settings(), transport=httpx.MockTransport(spotify))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


async def test_a_batch_flows_through_the_real_graph(
    live_client: httpx.AsyncClient, spotify: Any
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

    searches = [r for r in spotify.requests if r.url.path.endswith(SEARCH_PATH)]
    assert len(searches) == 2
    assert searches[0].url.params["market"] == "GB"
    assert searches[0].url.params["q"] == ('track:"Bohemian Rhapsody" artist:"Queen" year:1975')
    assert searches[0].headers["Authorization"] == "Bearer live-token"


async def test_the_token_is_fetched_once_for_a_whole_batch(
    live_client: httpx.AsyncClient, spotify: Any
) -> None:
    await live_client.post(
        "/v1/lookup", json={"items": [{"name": f"track {n}"} for n in range(10)]}
    )
    assert spotify.token_calls == 1


async def test_readiness_uses_the_real_token_provider(
    live_client: httpx.AsyncClient, spotify: Any
) -> None:
    response = await live_client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "dependencies": {"spotify": "ok"}}
    assert spotify.token_calls == 1


async def test_an_upstream_failure_surfaces_as_a_per_item_error(
    live_client: httpx.AsyncClient, spotify: Any
) -> None:
    spotify.search_status = 500
    response = await live_client.post("/v1/lookup", json={"items": [{"name": "Bohemian Rhapsody"}]})

    assert response.status_code == 200
    [result] = response.json()["results"]
    assert result["status"] == "error"
    assert "failing" in result["error"]
