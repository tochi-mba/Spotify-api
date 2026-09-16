"""Per-person settings over HTTP: two accounts differ, and a job keeps its submit timeout."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from keyring_client import JWKS_PATH
from keyring_client.testing import FakeKeyring, jwks
from settings_client.testing import FakeSettingsClient

from spotify_api.api.dependencies import get_resolver
from spotify_api.app import create_app
from spotify_api.preferences import PROFILE_UNKNOWN, REFUSED, build_preference_source
from tests.factories import make_settings, problem_type, user_token
from tests.integration.conftest import ACCOUNT, OTHER_ACCOUNT, FakeResolver, bearer, client_for

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

    from fastapi import FastAPI

ALICE = user_token(ACCOUNT)
BOB = user_token(OTHER_ACCOUNT)
ENDPOINT = "/v1/lookup"
TRACK = "spotify:track:7tFiyTwD0nx5a1eklYtX2J"
SPOTIFY_URL = "https://api.spotify.test/v1"


class TokenKeyedFake(FakeSettingsClient):
    """A fake that can answer differently for two tokens.

    The shared fake seeds one namespace for everybody. Two accounts having different
    markets or batch caps is the property this service has to prove, so the seed is
    keyed by token here.
    """

    def __init__(self) -> None:
        super().__init__()
        self.by_token: dict[str, dict[str, Any]] = {}

    def seed_token(self, user_token: str, values: Mapping[str, Any]) -> None:
        self.by_token[user_token] = dict(values)

    async def resolve(self, namespace: str, *, user_token: str) -> Any:
        if user_token in self.by_token:
            self._values[namespace] = dict(self.by_token[user_token])
        return await super().resolve(namespace, user_token=user_token)


@pytest.fixture
def resolver() -> FakeResolver:
    return FakeResolver()


@pytest.fixture
def keyring() -> FakeKeyring:
    return FakeKeyring()


@pytest.fixture
def chosen() -> TokenKeyedFake:
    return TokenKeyedFake()


@pytest.fixture
def app(resolver: FakeResolver, keyring: FakeKeyring, chosen: TokenKeyedFake) -> FastAPI:
    settings = make_settings(max_batch_size=50, confirm_timeout_seconds=30.0)
    application = create_app(
        settings=settings,
        keyring_transport=keyring.transport(),
        preferences=build_preference_source(settings, client=chosen),
    )
    application.dependency_overrides[get_resolver] = lambda: resolver
    return application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with client_for(app, ALICE) as http_client:
        yield http_client


class TestTwoAccounts:
    async def test_two_accounts_are_held_to_different_batch_caps(
        self, client: httpx.AsyncClient, app: FastAPI, chosen: TokenKeyedFake
    ) -> None:
        chosen.seed_token(ALICE, {"max_batch_size": 1})
        chosen.seed_token(BOB, {"max_batch_size": 3})
        two = {"items": [{"name": "a"}, {"name": "b"}]}

        alice = await client.post(ENDPOINT, json=two)
        async with client_for(app, BOB) as bob_client:
            bob = await bob_client.post(ENDPOINT, json=two)

        assert alice.status_code == 422
        assert "at most 1" in alice.json()["detail"]
        assert bob.status_code == 200

    async def test_two_accounts_resolve_against_different_markets(
        self,
        client: httpx.AsyncClient,
        app: FastAPI,
        resolver: FakeResolver,
        chosen: TokenKeyedFake,
    ) -> None:
        chosen.seed_token(ALICE, {"default_market": "PT"})
        chosen.seed_token(BOB, {"default_market": "GB"})
        body = {"items": [{"name": "x"}]}

        await client.post(ENDPOINT, json=body)
        async with client_for(app, BOB) as bob_client:
            await bob_client.post(ENDPOINT, json=body)

        assert resolver.markets == ["PT", "GB"]


class TestWhenSettingsApiIsUnwell:
    async def test_settings_api_refusing_this_service_is_a_503(
        self, client: httpx.AsyncClient, chosen: TokenKeyedFake
    ) -> None:
        chosen.rejects["spotify"] = (403, "spotify-api was not granted spotify")

        response = await client.post(ENDPOINT, json={"items": [{"name": "x"}]})

        assert response.status_code == 503
        assert response.headers["content-type"].startswith("application/problem+json")
        problem = response.json()
        assert problem["type"] == problem_type("preferences-unavailable")
        assert problem["detail"] == REFUSED
        assert "granted" not in problem["detail"]
        assert "spotify-api" not in problem["detail"]

    async def test_an_outage_refuses_an_unnamed_profile_and_not_a_named_one(
        self, client: httpx.AsyncClient, chosen: TokenKeyedFake
    ) -> None:
        chosen.unavailable = True

        unnamed = await client.post(ENDPOINT, json={"items": [{"name": "x"}]})
        named = await client.post(
            ENDPOINT,
            json={"items": [{"name": "x"}]},
            headers={"X-Keyring-Profile": "work"},
        )

        assert unnamed.status_code == 503
        assert unnamed.json()["detail"] == PROFILE_UNKNOWN
        assert named.status_code == 200


class TestANamedMarketWins:
    async def test_the_body_wins_over_the_persons_default(
        self, client: httpx.AsyncClient, resolver: FakeResolver, chosen: TokenKeyedFake
    ) -> None:
        chosen.seed_token(ALICE, {"default_market": "PT"})

        await client.post(ENDPOINT, json={"items": [{"name": "x"}], "market": "gb"})

        assert resolver.markets == ["GB"]


def player_state(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "is_playing": True,
        "progress_ms": 0,
        "shuffle_state": False,
        "repeat_state": "off",
        "device": {
            "id": "device-1",
            "name": "Kitchen",
            "type": "Speaker",
            "is_active": True,
            "volume_percent": 50,
            "supports_volume": True,
        },
        "item": {"uri": TRACK, "name": "Bohemian Rhapsody"},
        "currently_playing_type": "track",
    }
    base.update(overrides)
    return base


class FakeSpotify:
    """keyring plus a player that never confirms a pause, so a confirm timeout can fire."""

    def __init__(self) -> None:
        self.state: dict[str, Any] | None = player_state()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "keyring.test":
            if request.url.path == JWKS_PATH:
                return httpx.Response(200, json=jwks())
            return httpx.Response(
                200,
                json={
                    "service": "spotify",
                    "headers": {"Authorization": "Bearer spotify-token"},
                    "query_params": {},
                    "expires_at": None,
                },
            )
        path = request.url.path.removeprefix("/v1")
        if request.method == "GET" and path == "/me/player":
            return httpx.Response(200, json=self.state)
        return httpx.Response(204)


async def test_a_jobs_confirm_timeout_is_the_one_captured_at_submit() -> None:
    chosen = FakeSettingsClient()
    chosen.seed("spotify", {"confirm_timeout_seconds": 1})
    settings = make_settings(
        keyring_base_url="https://keyring.test",
        spotify_base_url=SPOTIFY_URL,
        confirm_poll_interval_seconds=0.05,
        confirm_timeout_seconds=30.0,
    )
    app = create_app(
        settings=settings,
        transport=httpx.MockTransport(FakeSpotify()),
        preferences=build_preference_source(settings, client=chosen),
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            headers=bearer(ALICE),
        ) as http_client:
            accepted = await http_client.post("/v1/player/pause?async=true")
            assert accepted.status_code == 202
            chosen.seed("spotify", {"confirm_timeout_seconds": 30})
            poll_url = accepted.json()["poll_url"]
            job: dict[str, Any] = {}
            for _ in range(40):
                await asyncio.sleep(0.1)
                job = (await http_client.get(poll_url)).json()
                if job["status"] not in ("pending", "running"):
                    break

    assert job["status"] == "failed"
    assert job["error_type"] == "confirmation_timeout"
    assert "1s" in job["error"]
    assert "30s" not in job["error"]
