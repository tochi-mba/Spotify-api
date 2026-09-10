"""The client survives the failure modes Spotify actually exhibits."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeAlias

import httpx
import pytest
from pydantic import SecretStr

from spotify_api.credentials.models import ResolvedCredential, UserContext
from spotify_api.errors import (
    KeyringUnavailableError,
    NoActiveDeviceError,
    PremiumRequiredError,
    SpotifyAuthError,
    SpotifyRateLimitError,
    SpotifyUnavailableError,
)
from spotify_api.models.requests import LookupItem
from spotify_api.spotify.client import SpotifyClient, SpotifyResponse
from tests.factories import make_settings

if TYPE_CHECKING:
    from collections.abc import Callable

Handler: TypeAlias = "Callable[[httpx.Request], httpx.Response]"

ITEM = LookupItem(name="Bohemian Rhapsody", artist="Queen")
CONTEXT = UserContext(user_token=SecretStr("user-token"), profile="personal")


class FakeCredentials:
    """Stands in for keyring, recording resolves and invalidations."""

    def __init__(self, tokens: list[str] | None = None) -> None:
        self.tokens = tokens or ["tok-1", "tok-2", "tok-3"]
        self.resolves = 0
        self.invalidations = 0
        self.healthy = True
        self.raises: Exception | None = None

    async def resolve(self, *, user_token: str, profile: str) -> ResolvedCredential:
        if self.raises is not None:
            raise self.raises
        index = min(self.invalidations, len(self.tokens) - 1)
        self.resolves += 1
        return ResolvedCredential(headers={"Authorization": f"Bearer {self.tokens[index]}"})

    def invalidate(self, *, user_token: str, profile: str) -> None:
        self.invalidations += 1

    async def check_health(self) -> bool:
        return self.healthy


class RecordingSleeper:
    def __init__(self) -> None:
        self.slept: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.slept.append(seconds)


@pytest.fixture
def sleeper() -> RecordingSleeper:
    return RecordingSleeper()


@pytest.fixture
def credentials() -> FakeCredentials:
    return FakeCredentials()


@pytest.fixture
def requests() -> list[httpx.Request]:
    return []


@pytest.fixture
def build_client(
    sleeper: RecordingSleeper, credentials: FakeCredentials, requests: list[httpx.Request]
) -> Callable[..., SpotifyClient]:
    def build(handler: Handler, **overrides: Any) -> SpotifyClient:
        def recording(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return handler(request)

        return SpotifyClient(
            client=httpx.AsyncClient(transport=httpx.MockTransport(recording)),
            credentials=credentials,
            settings=make_settings(**overrides),
            sleeper=sleeper,
        )

    return build


def responder(*responses: httpx.Response) -> Handler:
    """Play ``responses`` in order, repeating the last."""
    queue = list(responses)

    def handler(_: httpx.Request) -> httpx.Response:
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return handler


# -- the generic request ----------------------------------------------------


async def test_it_attaches_the_credential_keyring_supplied(
    build_client: Any, requests: list[httpx.Request]
) -> None:
    client = build_client(responder(httpx.Response(200, json={"ok": True})))
    await client.request("GET", "/me", context=CONTEXT)
    assert requests[0].headers["Authorization"] == "Bearer tok-1"


async def test_it_builds_the_url_from_the_configured_base(
    build_client: Any, requests: list[httpx.Request]
) -> None:
    client = build_client(
        responder(httpx.Response(200, json={})), spotify_api_base_url="https://api.test/v1"
    )
    await client.request("GET", "/me/player", context=CONTEXT)
    assert str(requests[0].url) == "https://api.test/v1/me/player"


async def test_it_returns_the_status_and_decoded_body(build_client: Any) -> None:
    client = build_client(responder(httpx.Response(200, json={"id": "abc"})))
    response = await client.request("GET", "/me", context=CONTEXT)

    assert isinstance(response, SpotifyResponse)
    assert response.status_code == 200
    assert response.body == {"id": "abc"}
    assert response.is_empty is False


async def test_a_204_is_a_successful_empty_response(build_client: Any) -> None:
    # Most player commands answer 204; that is success, not a missing body.
    client = build_client(responder(httpx.Response(204)))
    response = await client.request("PUT", "/me/player/play", context=CONTEXT)

    assert response.status_code == 204
    assert response.is_empty is True
    assert response.body is None


async def test_a_200_with_an_empty_body_is_tolerated(build_client: Any) -> None:
    client = build_client(responder(httpx.Response(200, content=b"")))
    assert (await client.request("PUT", "/me/player/pause", context=CONTEXT)).is_empty


async def test_a_json_body_is_sent_when_given(
    build_client: Any, requests: list[httpx.Request]
) -> None:
    client = build_client(responder(httpx.Response(204)))
    await client.request(
        "PUT", "/me/player/play", context=CONTEXT, json={"uris": ["spotify:track:x"]}
    )
    assert requests[0].read() == b'{"uris":["spotify:track:x"]}'


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"limit": 20}, "limit=20"),
        ({"shuffle_state": True}, "shuffle_state=true"),
        ({"shuffle_state": False}, "shuffle_state=false"),
        ({"ids": ["a", "b"]}, "ids=a%2Cb"),
        ({"market": None, "limit": 1}, "limit=1"),
    ],
)
async def test_query_parameters_are_encoded_the_way_spotify_expects(
    build_client: Any, requests: list[httpx.Request], params: dict[str, Any], expected: str
) -> None:
    client = build_client(responder(httpx.Response(200, json={})))
    await client.request("GET", "/search", context=CONTEXT, params=params)
    assert expected in str(requests[0].url)


async def test_a_none_parameter_is_dropped_entirely(
    build_client: Any, requests: list[httpx.Request]
) -> None:
    client = build_client(responder(httpx.Response(200, json={})))
    await client.request("GET", "/search", context=CONTEXT, params={"market": None})
    assert "market" not in str(requests[0].url)


async def test_credential_query_params_are_merged_in(
    build_client: Any, requests: list[httpx.Request], credentials: FakeCredentials
) -> None:
    async def resolve(*, user_token: str, profile: str) -> ResolvedCredential:  # noqa: ARG001
        return ResolvedCredential(headers={"Authorization": "Bearer t"}, query_params={"k": "v"})

    credentials.resolve = resolve  # type: ignore[method-assign]
    client = build_client(responder(httpx.Response(200, json={})))
    await client.request("GET", "/me", context=CONTEXT)
    assert "k=v" in str(requests[0].url)


# -- credential failures ----------------------------------------------------


async def test_a_keyring_failure_propagates_unchanged(
    build_client: Any, credentials: FakeCredentials
) -> None:
    credentials.raises = KeyringUnavailableError("keyring is down")
    client = build_client(responder(httpx.Response(200, json={})))
    with pytest.raises(KeyringUnavailableError):
        await client.request("GET", "/me", context=CONTEXT)


async def test_a_401_re_resolves_the_credential_once_and_retries(
    build_client: Any, credentials: FakeCredentials, requests: list[httpx.Request]
) -> None:
    client = build_client(responder(httpx.Response(401), httpx.Response(200, json={"ok": True})))
    assert (await client.request("GET", "/me", context=CONTEXT)).body == {"ok": True}

    assert credentials.invalidations == 1
    assert requests[1].headers["Authorization"] == "Bearer tok-2"


async def test_a_persistent_401_is_an_auth_error(
    build_client: Any, credentials: FakeCredentials
) -> None:
    client = build_client(responder(httpx.Response(401)))
    with pytest.raises(SpotifyAuthError):
        await client.request("GET", "/me", context=CONTEXT)
    assert credentials.invalidations == 1


async def test_re_resolving_does_not_consume_the_retry_budget(
    build_client: Any, requests: list[httpx.Request]
) -> None:
    client = build_client(
        responder(httpx.Response(401), httpx.Response(200, json={})), max_retries=0
    )
    await client.request("GET", "/me", context=CONTEXT)
    assert len(requests) == 2


# -- playback-specific failures ---------------------------------------------


async def test_a_403_naming_premium_says_so_plainly(build_client: Any) -> None:
    client = build_client(
        responder(
            httpx.Response(
                403,
                json={
                    "error": {"status": 403, "message": "Player command failed: Premium required"}
                },
            )
        )
    )
    with pytest.raises(PremiumRequiredError, match="Premium"):
        await client.request("PUT", "/me/player/play", context=CONTEXT)


async def test_any_other_403_stays_a_plain_authorisation_failure(build_client: Any) -> None:
    client = build_client(
        responder(httpx.Response(403, json={"error": {"message": "Insufficient client scope"}}))
    )
    with pytest.raises(SpotifyAuthError, match="refused"):
        await client.request("PUT", "/me/player/play", context=CONTEXT)


async def test_a_404_on_a_player_route_means_no_active_device(build_client: Any) -> None:
    client = build_client(
        responder(httpx.Response(404, json={"error": {"message": "No active device found"}}))
    )
    with pytest.raises(NoActiveDeviceError, match="no active Spotify device"):
        await client.request("PUT", "/me/player/play", context=CONTEXT)


async def test_a_404_elsewhere_is_a_missing_resource(build_client: Any) -> None:
    client = build_client(
        responder(httpx.Response(404, json={"error": {"message": "non existing id"}}))
    )
    with pytest.raises(SpotifyUnavailableError, match="no such resource"):
        await client.request("GET", "/albums/nope", context=CONTEXT)


async def test_a_bare_404_body_on_a_player_route_is_still_a_device_problem(
    build_client: Any,
) -> None:
    client = build_client(responder(httpx.Response(404, content=b"")))
    with pytest.raises(NoActiveDeviceError):
        await client.request("POST", "/me/player/next", context=CONTEXT)


async def test_a_non_dict_error_body_does_not_crash_the_translation(build_client: Any) -> None:
    client = build_client(responder(httpx.Response(404, json=["weird"])))
    with pytest.raises(SpotifyUnavailableError):
        await client.request("GET", "/albums/x", context=CONTEXT)


async def test_a_string_error_body_is_read_for_its_message(build_client: Any) -> None:
    client = build_client(responder(httpx.Response(403, json={"error": "premium required"})))
    with pytest.raises(PremiumRequiredError):
        await client.request("PUT", "/me/player/play", context=CONTEXT)


# -- the retry ladder -------------------------------------------------------


async def test_a_429_is_retried_after_the_interval_spotify_asks_for(
    build_client: Any, sleeper: RecordingSleeper
) -> None:
    client = build_client(
        responder(httpx.Response(429, headers={"Retry-After": "2"}), httpx.Response(200, json={}))
    )
    await client.request("GET", "/me", context=CONTEXT)
    assert sleeper.slept == [2.0]


async def test_a_429_without_retry_after_falls_back_to_backoff(
    build_client: Any, sleeper: RecordingSleeper
) -> None:
    client = build_client(
        responder(httpx.Response(429), httpx.Response(200, json={})),
        retry_backoff_base_seconds=0.5,
    )
    await client.request("GET", "/me", context=CONTEXT)
    assert sleeper.slept == [0.5]


async def test_an_unparseable_retry_after_falls_back_to_backoff(
    build_client: Any, sleeper: RecordingSleeper
) -> None:
    client = build_client(
        responder(
            httpx.Response(429, headers={"Retry-After": "later"}), httpx.Response(200, json={})
        ),
        retry_backoff_base_seconds=0.5,
    )
    await client.request("GET", "/me", context=CONTEXT)
    assert sleeper.slept == [0.5]


async def test_persistent_rate_limiting_exhausts_the_budget(
    build_client: Any, requests: list[httpx.Request]
) -> None:
    client = build_client(
        responder(httpx.Response(429, headers={"Retry-After": "1"})), max_retries=2
    )
    with pytest.raises(SpotifyRateLimitError) as excinfo:
        await client.request("GET", "/me", context=CONTEXT)
    assert excinfo.value.retry_after == 1.0
    assert len(requests) == 3


async def test_a_5xx_is_retried_with_exponential_backoff(
    build_client: Any, sleeper: RecordingSleeper
) -> None:
    client = build_client(
        responder(httpx.Response(500), httpx.Response(502), httpx.Response(200, json={})),
        retry_backoff_base_seconds=0.1,
    )
    await client.request("GET", "/me", context=CONTEXT)
    assert sleeper.slept == [0.1, 0.2]


async def test_persistent_5xx_exhausts_the_budget(
    build_client: Any, requests: list[httpx.Request]
) -> None:
    client = build_client(responder(httpx.Response(503)), max_retries=1)
    with pytest.raises(SpotifyUnavailableError, match="failing"):
        await client.request("GET", "/me", context=CONTEXT)
    assert len(requests) == 2


async def test_a_timeout_is_retried_then_reported_as_unreachable(
    build_client: Any, requests: list[httpx.Request]
) -> None:
    def timeout(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow")

    client = build_client(timeout, max_retries=1)
    with pytest.raises(SpotifyUnavailableError, match="unreachable"):
        await client.request("GET", "/me", context=CONTEXT)
    assert len(requests) == 2


async def test_a_transport_error_recovers_if_a_retry_succeeds(build_client: Any) -> None:
    attempts = {"n": 0}

    def flaky(_: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise httpx.ConnectError("reset")
        return httpx.Response(200, json={"ok": True})

    assert (await build_client(flaky).request("GET", "/me", context=CONTEXT)).body == {"ok": True}


async def test_retries_are_disabled_when_the_budget_is_zero(
    build_client: Any, requests: list[httpx.Request]
) -> None:
    client = build_client(responder(httpx.Response(500)), max_retries=0)
    with pytest.raises(SpotifyUnavailableError):
        await client.request("GET", "/me", context=CONTEXT)
    assert len(requests) == 1


async def test_an_unexpected_4xx_is_reported_with_spotifys_message(build_client: Any) -> None:
    client = build_client(responder(httpx.Response(400, json={"error": {"message": "invalid id"}})))
    with pytest.raises(SpotifyUnavailableError, match="rejected the request"):
        await client.request("GET", "/tracks/x", context=CONTEXT)


async def test_a_non_json_success_body_is_reported(build_client: Any) -> None:
    client = build_client(responder(httpx.Response(200, content=b"<html/>")))
    with pytest.raises(SpotifyUnavailableError, match="could not be understood"):
        await client.request("GET", "/me", context=CONTEXT)


async def test_backoff_is_capped(build_client: Any, sleeper: RecordingSleeper) -> None:
    client = build_client(
        responder(httpx.Response(500)), max_retries=10, retry_backoff_base_seconds=1.0
    )
    with pytest.raises(SpotifyUnavailableError):
        await client.request("GET", "/me", context=CONTEXT)
    assert max(sleeper.slept) <= SpotifyClient.MAX_BACKOFF_SECONDS


# -- search_track, now a caller of request ----------------------------------


async def test_search_track_returns_the_best_match(
    build_client: Any, search_found: dict[str, Any], requests: list[httpx.Request]
) -> None:
    client = build_client(responder(httpx.Response(200, json=search_found)))
    payload = await client.search_track(ITEM, market="GB", context=CONTEXT)

    assert payload is not None
    assert payload["id"] == "7tFiyTwD0nx5a1eklYtX2J"
    params = requests[0].url.params
    assert params["q"] == 'track:"Bohemian Rhapsody" artist:"Queen"'
    assert params["type"] == "track"
    assert params["limit"] == "1"
    assert params["market"] == "GB"


async def test_search_track_omits_the_market_when_not_asked(
    build_client: Any, search_found: dict[str, Any], requests: list[httpx.Request]
) -> None:
    client = build_client(responder(httpx.Response(200, json=search_found)))
    await client.search_track(ITEM, market=None, context=CONTEXT)
    assert "market" not in requests[0].url.params


async def test_search_track_returns_none_when_nothing_matches(
    build_client: Any, search_empty: dict[str, Any]
) -> None:
    client = build_client(responder(httpx.Response(200, json=search_empty)))
    assert await client.search_track(ITEM, market=None, context=CONTEXT) is None


async def test_search_track_rejects_a_body_that_is_not_an_object(build_client: Any) -> None:
    client = build_client(responder(httpx.Response(200, json=["unexpected"])))
    with pytest.raises(SpotifyUnavailableError, match="not a JSON object"):
        await client.search_track(ITEM, market=None, context=CONTEXT)


# -- health -----------------------------------------------------------------


@pytest.mark.parametrize("healthy", [True, False])
async def test_check_health_delegates_to_the_credential_source(
    build_client: Any, credentials: FakeCredentials, healthy: bool
) -> None:
    credentials.healthy = healthy
    assert await build_client(responder(httpx.Response(200, json={}))).check_health() is healthy
