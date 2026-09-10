"""Token acquisition is cached, refreshed before expiry, and stampede-safe."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, TypeAlias

import httpx
import pytest

from spotify_api.errors import SpotifyAuthError, SpotifyUnavailableError
from spotify_api.spotify.auth import ClientCredentialsProvider
from tests.factories import make_settings

if TYPE_CHECKING:
    from collections.abc import Callable

Handler: TypeAlias = "Callable[[httpx.Request], Any]"
ProviderFactory: TypeAlias = "Callable[..., ClientCredentialsProvider]"

TOKEN_URL = "https://accounts.spotify.com/api/token"


class FakeClock:
    """A hand-cranked clock, so expiry is tested directly rather than waited for."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def calls() -> list[httpx.Request]:
    return []


@pytest.fixture
def provider_factory(clock: FakeClock, calls: list[httpx.Request]) -> ProviderFactory:
    """Build a provider whose token endpoint is served by ``handler``."""

    def build(handler: Handler, **setting_overrides: Any) -> ClientCredentialsProvider:
        def recording(request: httpx.Request) -> Any:
            calls.append(request)
            return handler(request)

        return ClientCredentialsProvider(
            client=httpx.AsyncClient(transport=httpx.MockTransport(recording)),
            settings=make_settings(**setting_overrides),
            clock=clock,
        )

    return build


def ok_token(expires_in: int = 3600, token: str = "tok-1") -> Handler:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"access_token": token, "token_type": "Bearer", "expires_in": expires_in}
        )

    return handler


async def test_fetches_a_token_on_first_use(
    provider_factory: ProviderFactory, calls: list[httpx.Request]
) -> None:
    provider = provider_factory(ok_token())
    assert await provider.get_token() == "tok-1"
    assert len(calls) == 1


async def test_sends_basic_auth_and_the_client_credentials_grant(
    provider_factory: ProviderFactory, calls: list[httpx.Request]
) -> None:
    provider = provider_factory(ok_token())
    await provider.get_token()
    request = calls[0]
    assert str(request.url) == TOKEN_URL
    assert request.headers["Authorization"].startswith("Basic ")
    assert request.headers["Content-Type"] == "application/x-www-form-urlencoded"
    assert b"grant_type=client_credentials" in request.content


async def test_a_cached_token_is_reused(
    provider_factory: ProviderFactory, calls: list[httpx.Request]
) -> None:
    provider = provider_factory(ok_token())
    assert await provider.get_token() == "tok-1"
    assert await provider.get_token() == "tok-1"
    assert len(calls) == 1


async def test_the_token_is_refreshed_once_it_is_within_the_expiry_skew(
    provider_factory: ProviderFactory, calls: list[httpx.Request], clock: FakeClock
) -> None:
    tokens = iter(["tok-1", "tok-2"])

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"access_token": next(tokens), "token_type": "Bearer", "expires_in": 3600},
        )

    provider = provider_factory(handler, token_expiry_skew_seconds=60)
    assert await provider.get_token() == "tok-1"

    clock.advance(3600 - 61)  # still outside the skew window
    assert await provider.get_token() == "tok-1"

    clock.advance(2)  # now inside it
    assert await provider.get_token() == "tok-2"
    assert len(calls) == 2


async def test_concurrent_callers_trigger_exactly_one_token_fetch(
    provider_factory: ProviderFactory, calls: list[httpx.Request]
) -> None:
    async def slow(_: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.01)
        return httpx.Response(
            200, json={"access_token": "tok-1", "token_type": "Bearer", "expires_in": 3600}
        )

    provider = provider_factory(slow)
    results = await asyncio.gather(*(provider.get_token() for _ in range(20)))

    assert set(results) == {"tok-1"}
    assert len(calls) == 1


async def test_force_refresh_bypasses_the_cache(
    provider_factory: ProviderFactory, calls: list[httpx.Request]
) -> None:
    tokens = iter(["tok-1", "tok-2"])

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"access_token": next(tokens), "token_type": "Bearer", "expires_in": 3600}
        )

    provider = provider_factory(handler)
    assert await provider.get_token() == "tok-1"
    assert await provider.get_token(force_refresh=True) == "tok-2"
    assert len(calls) == 2


@pytest.mark.parametrize("status", [400, 401, 403])
async def test_rejected_credentials_raise_an_auth_error(
    provider_factory: ProviderFactory, status: int
) -> None:
    provider = provider_factory(lambda _: httpx.Response(status, json={"error": "invalid_client"}))
    with pytest.raises(SpotifyAuthError, match="rejected"):
        await provider.get_token()


async def test_a_server_error_from_the_token_endpoint_is_unavailability(
    provider_factory: ProviderFactory,
) -> None:
    provider = provider_factory(lambda _: httpx.Response(503, text="down"))
    with pytest.raises(SpotifyUnavailableError):
        await provider.get_token()


async def test_a_transport_failure_is_unavailability(provider_factory: ProviderFactory) -> None:
    def boom(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    provider = provider_factory(boom)
    with pytest.raises(SpotifyUnavailableError):
        await provider.get_token()


@pytest.mark.parametrize(
    "body",
    [
        {"token_type": "Bearer", "expires_in": 3600},
        {"access_token": "", "expires_in": 3600},
        {"access_token": "tok", "expires_in": "soon"},
    ],
)
async def test_a_malformed_token_grant_is_an_auth_error(
    provider_factory: ProviderFactory, body: dict[str, Any]
) -> None:
    provider = provider_factory(lambda _: httpx.Response(200, json=body))
    with pytest.raises(SpotifyAuthError):
        await provider.get_token()


async def test_a_non_json_token_response_is_an_auth_error(
    provider_factory: ProviderFactory,
) -> None:
    provider = provider_factory(lambda _: httpx.Response(200, text="<html>nope</html>"))
    with pytest.raises(SpotifyAuthError):
        await provider.get_token()


async def test_a_failed_fetch_does_not_poison_the_cache(
    provider_factory: ProviderFactory, calls: list[httpx.Request]
) -> None:
    responses = iter(
        [
            httpx.Response(503),
            httpx.Response(
                200, json={"access_token": "tok-1", "token_type": "Bearer", "expires_in": 3600}
            ),
        ]
    )
    provider = provider_factory(lambda _: next(responses))

    with pytest.raises(SpotifyUnavailableError):
        await provider.get_token()
    assert await provider.get_token() == "tok-1"
    assert len(calls) == 2
