"""Liveness is dependency-free; readiness reflects the upstream."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

import spotify_api

if TYPE_CHECKING:
    import httpx

    from tests.integration.conftest import FakeResolver


async def test_healthy_returns_the_documented_json(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthy")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["service"] == "spotify-api"
    assert payload["version"] == spotify_api.__version__
    assert payload["uptime_seconds"] >= 0


async def test_healthy_never_consults_the_upstream(
    client: httpx.AsyncClient, resolver: FakeResolver
) -> None:
    # A liveness probe that can be failed by a third party is not a liveness
    # probe. Even with the upstream down, /healthy must answer 200.
    resolver.healthy = False
    assert (await client.get("/healthy")).status_code == 200


async def test_healthy_echoes_a_request_id(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthy", headers={"X-Request-ID": "probe-1"})
    assert response.headers["X-Request-ID"] == "probe-1"


async def test_uptime_increases(client: httpx.AsyncClient) -> None:
    first = (await client.get("/healthy")).json()["uptime_seconds"]
    second = (await client.get("/healthy")).json()["uptime_seconds"]
    assert second >= first


@pytest.mark.parametrize(
    ("healthy", "status_code", "body"),
    [
        (True, 200, {"status": "ready", "dependencies": {"spotify": "ok"}}),
        (False, 503, {"status": "not_ready", "dependencies": {"spotify": "unavailable"}}),
    ],
)
async def test_ready_reflects_the_state_of_spotify(
    client: httpx.AsyncClient,
    resolver: FakeResolver,
    healthy: bool,
    status_code: int,
    body: dict[str, object],
) -> None:
    resolver.healthy = healthy
    response = await client.get("/ready")

    assert response.status_code == status_code
    assert response.json() == body
