"""The search client survives the failure modes Spotify actually exhibits."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeAlias

import httpx
import pytest

from spotify_api.errors import (
    SpotifyAuthError,
    SpotifyRateLimitError,
    SpotifyUnavailableError,
)
from spotify_api.models.requests import LookupItem
from spotify_api.spotify.client import SpotifyClient
from tests.factories import make_settings

if TYPE_CHECKING:
    from collections.abc import Callable

Handler: TypeAlias = "Callable[[httpx.Request], httpx.Response]"

ITEM = LookupItem(name="Bohemian Rhapsody", artist="Queen")


class FakeTokenProvider:
    """Hands out tokens and records how often a refresh was forced."""

    def __init__(self, tokens: list[str] | None = None) -> None:
        self.tokens = tokens or ["tok-1", "tok-2", "tok-3"]
        self.calls: list[bool] = []

    async def get_token(self, *, force_refresh: bool = False) -> str:
        self.calls.append(force_refresh)
        return self.tokens[min(len(self.calls) - 1, len(self.tokens) - 1)]


class RecordingSleeper:
    """Captures how long the client would have slept, without sleeping."""

    def __init__(self) -> None:
        self.slept: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.slept.append(seconds)


@pytest.fixture
def sleeper() -> RecordingSleeper:
    return RecordingSleeper()


@pytest.fixture
def token_provider() -> FakeTokenProvider:
    return FakeTokenProvider()


@pytest.fixture
def requests() -> list[httpx.Request]:
    return []


@pytest.fixture
def build_client(
    sleeper: RecordingSleeper, token_provider: FakeTokenProvider, requests: list[httpx.Request]
) -> Callable[..., SpotifyClient]:
    def build(handler: Handler, **overrides: Any) -> SpotifyClient:
        def recording(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return handler(request)

        return SpotifyClient(
            client=httpx.AsyncClient(transport=httpx.MockTransport(recording)),
            token_provider=token_provider,
            settings=make_settings(**overrides),
            sleeper=sleeper,
        )

    return build


def responder(*responses: httpx.Response) -> Handler:
    """Return a handler that plays ``responses`` in order, repeating the last."""
    queue = list(responses)

    def handler(_: httpx.Request) -> httpx.Response:
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return handler


async def test_a_successful_search_returns_the_raw_payload(
    build_client: Any, search_found: dict[str, Any], requests: list[httpx.Request]
) -> None:
    client = build_client(responder(httpx.Response(200, json=search_found)))
    payload = await client.search_track(ITEM, market=None)

    assert payload is not None
    assert payload["id"] == "7tFiyTwD0nx5a1eklYtX2J"
    assert requests[0].headers["Authorization"] == "Bearer tok-1"


async def test_the_request_carries_the_field_filtered_query(
    build_client: Any, search_found: dict[str, Any], requests: list[httpx.Request]
) -> None:
    client = build_client(responder(httpx.Response(200, json=search_found)))
    await client.search_track(ITEM, market="GB")

    params = requests[0].url.params
    assert params["q"] == 'track:"Bohemian Rhapsody" artist:"Queen"'
    assert params["type"] == "track"
    assert params["limit"] == "1"
    assert params["market"] == "GB"


async def test_market_is_omitted_when_not_requested(
    build_client: Any, search_found: dict[str, Any], requests: list[httpx.Request]
) -> None:
    client = build_client(responder(httpx.Response(200, json=search_found)))
    await client.search_track(ITEM, market=None)
    assert "market" not in requests[0].url.params


async def test_no_match_returns_none_rather_than_raising(
    build_client: Any, search_empty: dict[str, Any]
) -> None:
    client = build_client(responder(httpx.Response(200, json=search_empty)))
    assert await client.search_track(ITEM, market=None) is None


async def test_an_expired_token_is_refreshed_once_and_the_call_retried(
    build_client: Any,
    search_found: dict[str, Any],
    token_provider: FakeTokenProvider,
    requests: list[httpx.Request],
) -> None:
    client = build_client(
        responder(
            httpx.Response(401, json={"error": {"message": "expired"}}),
            httpx.Response(200, json=search_found),
        )
    )
    assert await client.search_track(ITEM, market=None) is not None
    assert token_provider.calls == [False, True]
    assert requests[1].headers["Authorization"] == "Bearer tok-2"


async def test_a_persistent_401_becomes_an_auth_error(
    build_client: Any, token_provider: FakeTokenProvider
) -> None:
    client = build_client(responder(httpx.Response(401, json={"error": "nope"})))
    with pytest.raises(SpotifyAuthError):
        await client.search_track(ITEM, market=None)
    assert token_provider.calls == [False, True]


async def test_a_403_is_an_auth_error_and_is_not_retried(
    build_client: Any, requests: list[httpx.Request]
) -> None:
    client = build_client(responder(httpx.Response(403, json={"error": "forbidden"})))
    with pytest.raises(SpotifyAuthError):
        await client.search_track(ITEM, market=None)
    assert len(requests) == 1


async def test_a_429_is_retried_after_the_interval_spotify_asks_for(
    build_client: Any, search_found: dict[str, Any], sleeper: RecordingSleeper
) -> None:
    client = build_client(
        responder(
            httpx.Response(429, headers={"Retry-After": "2"}),
            httpx.Response(200, json=search_found),
        )
    )
    assert await client.search_track(ITEM, market=None) is not None
    assert sleeper.slept == [2.0]


async def test_a_429_without_retry_after_falls_back_to_backoff(
    build_client: Any, search_found: dict[str, Any], sleeper: RecordingSleeper
) -> None:
    client = build_client(
        responder(httpx.Response(429), httpx.Response(200, json=search_found)),
        retry_backoff_base_seconds=0.5,
    )
    assert await client.search_track(ITEM, market=None) is not None
    assert sleeper.slept == [0.5]


async def test_an_unparseable_retry_after_falls_back_to_backoff(
    build_client: Any, search_found: dict[str, Any], sleeper: RecordingSleeper
) -> None:
    client = build_client(
        responder(
            httpx.Response(429, headers={"Retry-After": "later"}),
            httpx.Response(200, json=search_found),
        ),
        retry_backoff_base_seconds=0.5,
    )
    assert await client.search_track(ITEM, market=None) is not None
    assert sleeper.slept == [0.5]


async def test_persistent_rate_limiting_exhausts_the_budget_and_raises(
    build_client: Any, requests: list[httpx.Request]
) -> None:
    client = build_client(
        responder(httpx.Response(429, headers={"Retry-After": "1"})), max_retries=2
    )
    with pytest.raises(SpotifyRateLimitError) as excinfo:
        await client.search_track(ITEM, market=None)
    assert excinfo.value.retry_after == 1.0
    assert len(requests) == 3  # the original attempt plus two retries


async def test_a_5xx_is_retried_with_exponential_backoff(
    build_client: Any, search_found: dict[str, Any], sleeper: RecordingSleeper
) -> None:
    client = build_client(
        responder(httpx.Response(500), httpx.Response(502), httpx.Response(200, json=search_found)),
        retry_backoff_base_seconds=0.1,
    )
    assert await client.search_track(ITEM, market=None) is not None
    assert sleeper.slept == [0.1, 0.2]


async def test_persistent_5xx_exhausts_the_budget_and_raises(
    build_client: Any, requests: list[httpx.Request]
) -> None:
    client = build_client(responder(httpx.Response(503)), max_retries=1)
    with pytest.raises(SpotifyUnavailableError):
        await client.search_track(ITEM, market=None)
    assert len(requests) == 2


async def test_a_timeout_is_retried_then_reported_as_unavailable(
    build_client: Any, requests: list[httpx.Request]
) -> None:
    def timeout(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow")

    client = build_client(timeout, max_retries=1)
    with pytest.raises(SpotifyUnavailableError, match="unreachable"):
        await client.search_track(ITEM, market=None)
    assert len(requests) == 2


async def test_a_transport_error_recovers_if_a_retry_succeeds(
    build_client: Any, search_found: dict[str, Any]
) -> None:
    attempts = {"n": 0}

    def flaky(_: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise httpx.ConnectError("reset")
        return httpx.Response(200, json=search_found)

    client = build_client(flaky)
    assert await client.search_track(ITEM, market=None) is not None


async def test_retries_are_disabled_when_the_budget_is_zero(
    build_client: Any, requests: list[httpx.Request]
) -> None:
    client = build_client(responder(httpx.Response(500)), max_retries=0)
    with pytest.raises(SpotifyUnavailableError):
        await client.search_track(ITEM, market=None)
    assert len(requests) == 1


async def test_an_unexpected_4xx_is_reported_as_a_lookup_failure(build_client: Any) -> None:
    client = build_client(responder(httpx.Response(400, json={"error": "bad query"})))
    with pytest.raises(SpotifyUnavailableError, match="unexpected"):
        await client.search_track(ITEM, market=None)


async def test_a_non_json_success_body_is_reported_rather_than_crashing(
    build_client: Any,
) -> None:
    client = build_client(responder(httpx.Response(200, text="<html/>")))
    with pytest.raises(SpotifyUnavailableError, match="could not be understood"):
        await client.search_track(ITEM, market=None)


async def test_backoff_is_capped_so_a_long_budget_cannot_stall_a_request(
    build_client: Any, sleeper: RecordingSleeper
) -> None:
    client = build_client(
        responder(httpx.Response(500)), max_retries=10, retry_backoff_base_seconds=1.0
    )
    with pytest.raises(SpotifyUnavailableError):
        await client.search_track(ITEM, market=None)
    assert max(sleeper.slept) <= SpotifyClient.MAX_BACKOFF_SECONDS


async def test_a_token_refresh_does_not_consume_the_retry_budget(
    build_client: Any, search_found: dict[str, Any], requests: list[httpx.Request]
) -> None:
    # With no retries configured, a stale token must still be refreshed once:
    # re-authenticating is not a "retry" of a failed call, it is a correction.
    client = build_client(
        responder(httpx.Response(401), httpx.Response(200, json=search_found)), max_retries=0
    )
    assert await client.search_track(ITEM, market=None) is not None
    assert len(requests) == 2


async def test_a_401_with_no_retry_budget_is_never_mistaken_for_rate_limiting(
    build_client: Any,
) -> None:
    client = build_client(responder(httpx.Response(401)), max_retries=0)
    with pytest.raises(SpotifyAuthError):
        await client.search_track(ITEM, market=None)


async def test_check_health_is_true_when_a_token_can_be_obtained(build_client: Any) -> None:
    client = build_client(responder(httpx.Response(200, json={})))
    assert await client.check_health() is True


@pytest.mark.parametrize(
    "failure", [SpotifyAuthError("bad creds"), SpotifyUnavailableError("down")]
)
async def test_check_health_is_false_when_the_token_cannot_be_obtained(
    build_client: Any, token_provider: FakeTokenProvider, failure: Exception
) -> None:
    async def refuse(**_kwargs: bool) -> str:
        raise failure

    client = build_client(responder(httpx.Response(200, json={})))
    token_provider.get_token = refuse  # type: ignore[method-assign]
    assert await client.check_health() is False


async def test_a_json_body_that_is_not_an_object_is_rejected(build_client: Any) -> None:
    # A proxy or error page can return valid JSON that is not a search envelope.
    client = build_client(responder(httpx.Response(200, json=["unexpected"])))
    with pytest.raises(SpotifyUnavailableError, match="not a JSON object"):
        await client.search_track(ITEM, market=None)
