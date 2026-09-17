"""Credentials come from keyring; this service stores no Spotify secret."""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from spotify_api.credentials.keyring import KeyringCredentialProvider
from spotify_api.credentials.models import ResolvedCredential
from spotify_api.errors import (
    CredentialUnavailableError,
    KeyringUnavailableError,
    UserTokenRejectedError,
)
from tests.factories import TEST_SERVICE_TOKEN, make_settings

if TYPE_CHECKING:
    from collections.abc import Callable

type Handler = "Callable[[httpx.Request], httpx.Response]"

USER_TOKEN = "user-token-abc"
PROFILE = "personal"


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def grant(token: str = "spotify-token-1", expires_in: float | None = 3600.0) -> dict[str, Any]:
    expires_at = (
        None
        if expires_in is None
        else (dt.datetime.now(tz=dt.UTC) + dt.timedelta(seconds=expires_in)).isoformat()
    )
    return {
        "service": "spotify",
        "headers": {"Authorization": f"Bearer {token}"},
        "query_params": {},
        "expires_at": expires_at,
    }


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def calls() -> list[httpx.Request]:
    return []


@pytest.fixture
def build_provider(
    clock: FakeClock, calls: list[httpx.Request]
) -> Callable[..., KeyringCredentialProvider]:
    def build(handler: Handler, **overrides: Any) -> KeyringCredentialProvider:
        def recording(request: httpx.Request) -> Any:
            calls.append(request)
            return handler(request)

        return KeyringCredentialProvider(
            client=httpx.AsyncClient(transport=httpx.MockTransport(recording)),
            settings=make_settings(**overrides),
            clock=clock,
        )

    return build


def always(response: httpx.Response) -> Handler:
    return lambda _: response


# -- the request keyring receives ------------------------------------------


async def test_it_asks_keyring_for_the_spotify_credential(
    build_provider: Any, calls: list[httpx.Request]
) -> None:
    provider = build_provider(
        always(httpx.Response(200, json=grant())),
        keyring_base_url="https://keyring.test",
    )
    await provider.resolve(user_token=USER_TOKEN, profile=PROFILE)

    request = calls[0]
    assert request.method == "GET"
    assert str(request.url) == "https://keyring.test/v1/internal/credentials/personal/spotify"


async def test_it_presents_both_its_own_token_and_the_users(
    build_provider: Any, calls: list[httpx.Request]
) -> None:
    # keyring requires both: our identity as a service, and whose data we act on.
    provider = build_provider(always(httpx.Response(200, json=grant())))
    await provider.resolve(user_token=USER_TOKEN, profile=PROFILE)

    request = calls[0]
    assert request.headers["Authorization"] == f"Bearer {TEST_SERVICE_TOKEN}"
    assert request.headers["X-Keyring-User-Token"] == USER_TOKEN


async def test_it_returns_the_headers_keyring_says_to_attach(build_provider: Any) -> None:
    provider = build_provider(always(httpx.Response(200, json=grant("abc"))))
    resolved = await provider.resolve(user_token=USER_TOKEN, profile=PROFILE)

    assert isinstance(resolved, ResolvedCredential)
    assert resolved.headers == {"Authorization": "Bearer abc"}
    assert resolved.query_params == {}


async def test_query_params_are_carried_through(build_provider: Any) -> None:
    payload = grant()
    payload["query_params"] = {"api_key": "k"}
    provider = build_provider(always(httpx.Response(200, json=payload)))
    resolved = await provider.resolve(user_token=USER_TOKEN, profile=PROFILE)
    assert resolved.query_params == {"api_key": "k"}


# -- caching ----------------------------------------------------------------


async def test_a_resolved_credential_is_cached(
    build_provider: Any, calls: list[httpx.Request]
) -> None:
    # Without this a 50-item batch would make 50 keyring round-trips.
    provider = build_provider(always(httpx.Response(200, json=grant())))
    first = await provider.resolve(user_token=USER_TOKEN, profile=PROFILE)
    second = await provider.resolve(user_token=USER_TOKEN, profile=PROFILE)

    assert first.headers == second.headers
    assert len(calls) == 1


async def test_different_users_do_not_share_a_cache_entry(
    build_provider: Any, calls: list[httpx.Request]
) -> None:
    tokens = iter(["tok-a", "tok-b"])
    provider = build_provider(lambda _: httpx.Response(200, json=grant(next(tokens))))
    a = await provider.resolve(user_token="user-a", profile=PROFILE)
    b = await provider.resolve(user_token="user-b", profile=PROFILE)

    assert a.headers != b.headers
    assert len(calls) == 2


async def test_different_profiles_do_not_share_a_cache_entry(
    build_provider: Any, calls: list[httpx.Request]
) -> None:
    provider = build_provider(always(httpx.Response(200, json=grant())))
    await provider.resolve(user_token=USER_TOKEN, profile="personal")
    await provider.resolve(user_token=USER_TOKEN, profile="work")
    assert len(calls) == 2


async def test_the_cache_expires_within_the_skew(
    build_provider: Any, calls: list[httpx.Request], clock: FakeClock
) -> None:
    provider = build_provider(
        always(httpx.Response(200, json=grant(expires_in=3600))),
        credential_cache_skew_seconds=60,
    )
    await provider.resolve(user_token=USER_TOKEN, profile=PROFILE)

    clock.advance(3600 - 61)
    await provider.resolve(user_token=USER_TOKEN, profile=PROFILE)
    assert len(calls) == 1

    clock.advance(2)
    await provider.resolve(user_token=USER_TOKEN, profile=PROFILE)
    assert len(calls) == 2


async def test_a_credential_with_no_expiry_uses_the_default_ttl(
    build_provider: Any, calls: list[httpx.Request], clock: FakeClock
) -> None:
    # An API key has no expiry; cache it briefly rather than forever, so a
    # revocation at keyring takes effect in seconds rather than never.
    provider = build_provider(
        always(httpx.Response(200, json=grant(expires_in=None))),
        credential_cache_default_ttl_seconds=30,
    )
    await provider.resolve(user_token=USER_TOKEN, profile=PROFILE)
    clock.advance(29)
    await provider.resolve(user_token=USER_TOKEN, profile=PROFILE)
    assert len(calls) == 1

    clock.advance(2)
    await provider.resolve(user_token=USER_TOKEN, profile=PROFILE)
    assert len(calls) == 2


async def test_concurrent_callers_trigger_exactly_one_keyring_call(
    build_provider: Any, calls: list[httpx.Request]
) -> None:
    async def slow(_: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.01)
        return httpx.Response(200, json=grant())

    provider = build_provider(slow)
    results = await asyncio.gather(
        *(provider.resolve(user_token=USER_TOKEN, profile=PROFILE) for _ in range(20))
    )

    assert {r.headers["Authorization"] for r in results} == {"Bearer spotify-token-1"}
    assert len(calls) == 1


async def test_invalidate_forces_the_next_resolve_to_ask_again(
    build_provider: Any, calls: list[httpx.Request]
) -> None:
    provider = build_provider(always(httpx.Response(200, json=grant())))
    await provider.resolve(user_token=USER_TOKEN, profile=PROFILE)
    provider.invalidate(user_token=USER_TOKEN, profile=PROFILE)
    await provider.resolve(user_token=USER_TOKEN, profile=PROFILE)
    assert len(calls) == 2


async def test_invalidating_an_unknown_entry_is_harmless(build_provider: Any) -> None:
    provider = build_provider(always(httpx.Response(200, json=grant())))
    provider.invalidate(user_token="never-seen", profile=PROFILE)


# -- failures ---------------------------------------------------------------


@pytest.mark.parametrize("status", [401, 403])
async def test_a_rejected_user_token_is_reported_as_such(build_provider: Any, status: int) -> None:
    provider = build_provider(always(httpx.Response(status, json={"detail": "nope"})))
    with pytest.raises(UserTokenRejectedError):
        await provider.resolve(user_token=USER_TOKEN, profile=PROFILE)


async def test_an_unknown_profile_or_connection_is_a_missing_credential(
    build_provider: Any,
) -> None:
    provider = build_provider(always(httpx.Response(404, json={"detail": "no such profile"})))
    with pytest.raises(CredentialUnavailableError, match="not connected"):
        await provider.resolve(user_token=USER_TOKEN, profile=PROFILE)


async def test_a_credential_keyring_cannot_make_usable_is_reported_clearly(
    build_provider: Any,
) -> None:
    # keyring returns 503 when the vault is sealed or a refresh failed -- the
    # user must reconnect Spotify, which is not something we can retry past.
    provider = build_provider(
        always(httpx.Response(503, json={"detail": "the grant was revoked at the provider"}))
    )
    with pytest.raises(CredentialUnavailableError, match="revoked"):
        await provider.resolve(user_token=USER_TOKEN, profile=PROFILE)


async def test_keyring_being_down_is_distinct_from_a_bad_credential(
    build_provider: Any,
) -> None:
    provider = build_provider(always(httpx.Response(500, text="boom")))
    with pytest.raises(KeyringUnavailableError):
        await provider.resolve(user_token=USER_TOKEN, profile=PROFILE)


async def test_keyring_being_unreachable_is_reported(build_provider: Any) -> None:
    def boom(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    provider = build_provider(boom)
    with pytest.raises(KeyringUnavailableError, match="unreachable"):
        await provider.resolve(user_token=USER_TOKEN, profile=PROFILE)


@pytest.mark.parametrize(
    "body", [{"service": "spotify"}, {"headers": "not-a-dict"}, {"headers": {}}]
)
async def test_a_malformed_keyring_response_is_rejected(
    build_provider: Any, body: dict[str, Any]
) -> None:
    provider = build_provider(always(httpx.Response(200, json=body)))
    with pytest.raises(KeyringUnavailableError, match="could not be understood"):
        await provider.resolve(user_token=USER_TOKEN, profile=PROFILE)


async def test_a_non_json_keyring_response_is_rejected(build_provider: Any) -> None:
    provider = build_provider(always(httpx.Response(200, text="<html/>")))
    with pytest.raises(KeyringUnavailableError):
        await provider.resolve(user_token=USER_TOKEN, profile=PROFILE)


async def test_a_failed_resolve_does_not_poison_the_cache(
    build_provider: Any, calls: list[httpx.Request]
) -> None:
    responses = iter([httpx.Response(500), httpx.Response(200, json=grant())])
    provider = build_provider(lambda _: next(responses))

    with pytest.raises(KeyringUnavailableError):
        await provider.resolve(user_token=USER_TOKEN, profile=PROFILE)
    resolved = await provider.resolve(user_token=USER_TOKEN, profile=PROFILE)

    assert resolved.headers["Authorization"] == "Bearer spotify-token-1"
    assert len(calls) == 2


# -- health -----------------------------------------------------------------


async def test_check_health_is_true_when_keyring_answers(build_provider: Any) -> None:
    provider = build_provider(always(httpx.Response(200, json={"status": "ok"})))
    assert await provider.check_health() is True


async def test_check_health_is_false_when_keyring_is_down(build_provider: Any) -> None:
    provider = build_provider(always(httpx.Response(503, text="down")))
    assert await provider.check_health() is False


async def test_check_health_never_raises(build_provider: Any) -> None:
    def boom(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    assert await build_provider(boom).check_health() is False


async def test_check_health_probes_the_published_keys_not_keyrings_own_health(
    build_provider: Any, calls: list[httpx.Request]
) -> None:
    # keyring's /healthy answers 503 whenever any stored connection anywhere is unusable, so
    # probing it would take this service out of rotation over one person's expired grant.
    # The key document is what this service needs from keyring before it can serve anybody.
    provider = build_provider(always(httpx.Response(200, json={"keys": []})))

    assert await provider.check_health() is True
    assert calls[0].url.path == "/.well-known/jwks.json"


async def test_an_unparseable_expiry_falls_back_to_the_default_ttl(
    build_provider: Any, calls: list[httpx.Request], clock: FakeClock
) -> None:
    payload = grant()
    payload["expires_at"] = "not a timestamp"
    provider = build_provider(
        always(httpx.Response(200, json=payload)), credential_cache_default_ttl_seconds=30
    )
    await provider.resolve(user_token=USER_TOKEN, profile=PROFILE)
    clock.advance(29)
    await provider.resolve(user_token=USER_TOKEN, profile=PROFILE)
    assert len(calls) == 1


async def test_an_already_expired_credential_is_not_cached(
    build_provider: Any, calls: list[httpx.Request]
) -> None:
    provider = build_provider(always(httpx.Response(200, json=grant(expires_in=-10))))
    await provider.resolve(user_token=USER_TOKEN, profile=PROFILE)
    await provider.resolve(user_token=USER_TOKEN, profile=PROFILE)
    assert len(calls) == 2


async def test_a_zulu_suffixed_expiry_is_understood(
    build_provider: Any, calls: list[httpx.Request], clock: FakeClock
) -> None:
    # keyring's own examples render expiries as "...Z", which datetime.fromisoformat
    # did not accept before 3.11 and still deserves a test.
    payload = grant()
    payload["expires_at"] = (
        (dt.datetime.now(tz=dt.UTC) + dt.timedelta(hours=1))
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    provider = build_provider(always(httpx.Response(200, json=payload)))

    await provider.resolve(user_token=USER_TOKEN, profile=PROFILE)
    clock.advance(60)
    await provider.resolve(user_token=USER_TOKEN, profile=PROFILE)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(503, text="<html>sealed</html>"),
        httpx.Response(503, json=["not", "a", "dict"]),
        httpx.Response(503, json={"detail": {"nested": "object"}}),
    ],
)
async def test_a_503_without_a_readable_detail_is_still_reported(
    build_provider: Any, response: httpx.Response
) -> None:
    provider = build_provider(always(response))
    with pytest.raises(CredentialUnavailableError):
        await provider.resolve(user_token=USER_TOKEN, profile=PROFILE)
