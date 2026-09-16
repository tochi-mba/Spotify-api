"""Batch resolution is ordered, bounded, and isolates per-item failures."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from spotify_api.credentials.models import UserContext
from spotify_api.errors import (
    CredentialUnavailableError,
    KeyringUnavailableError,
    SpotifyAuthError,
    SpotifyRateLimitError,
    SpotifyUnavailableError,
    UserTokenRejectedError,
)
from spotify_api.models.requests import LookupItem
from spotify_api.models.responses import LookupStatus
from spotify_api.spotify.resolver import SpotifyTrackResolver
from tests.factories import make_settings

CONTEXT = UserContext(
    account_id="account-a", user_token=SecretStr("user-token"), profile="personal"
)

if TYPE_CHECKING:
    from collections.abc import Callable


class FakeSearchClient:
    """Stands in for SpotifyClient, driven by a per-name script."""

    def __init__(
        self,
        script: dict[str, Any] | None = None,
        *,
        default: Any = None,
        healthy: bool = True,
    ) -> None:
        self.script = script or {}
        self.default = default
        self.healthy = healthy
        self.seen: list[str] = []
        self.in_flight = 0
        self.peak_in_flight = 0
        self.delay = 0.0

    async def search_track(
        self, item: LookupItem, *, market: str | None, context: UserContext
    ) -> Any:
        self.seen.append(item.name)
        self.in_flight += 1
        self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            outcome = self.script.get(item.name, self.default)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        finally:
            self.in_flight -= 1

    async def check_health(self) -> bool:
        return self.healthy


@pytest.fixture
def raw_track(search_found: dict[str, Any]) -> dict[str, Any]:
    track: dict[str, Any] = search_found["tracks"]["items"][0]
    return track


@pytest.fixture
def build_resolver() -> Callable[..., SpotifyTrackResolver]:
    def build(client: FakeSearchClient, **overrides: Any) -> SpotifyTrackResolver:
        return SpotifyTrackResolver(client=client, settings=make_settings(**overrides))  # type: ignore[arg-type]

    return build


async def test_a_found_track_is_mapped_onto_the_result(
    build_resolver: Any, raw_track: dict[str, Any]
) -> None:
    resolver = build_resolver(FakeSearchClient(default=raw_track))
    [result] = await resolver.resolve(context=CONTEXT, items=[LookupItem(name="Bohemian Rhapsody")])

    assert result.index == 0
    assert result.status is LookupStatus.FOUND
    assert result.track is not None
    assert result.track.name == "Bohemian Rhapsody"
    assert result.error is None


async def test_no_match_is_reported_as_not_found(build_resolver: Any) -> None:
    resolver = build_resolver(FakeSearchClient(default=None))
    [result] = await resolver.resolve(context=CONTEXT, items=[LookupItem(name="nothing here")])

    assert result.status is LookupStatus.NOT_FOUND
    assert result.track is None
    assert result.error is None


async def test_results_keep_the_submitted_order_and_echo_the_query(
    build_resolver: Any, raw_track: dict[str, Any]
) -> None:
    client = FakeSearchClient({"b": raw_track}, default=None)
    client.delay = 0.01  # force interleaving, so ordering cannot be accidental
    resolver = build_resolver(client)

    items = [LookupItem(name=name) for name in ("a", "b", "c")]
    results = await resolver.resolve(items, context=CONTEXT)

    assert [r.index for r in results] == [0, 1, 2]
    assert [r.query.name for r in results] == ["a", "b", "c"]
    assert [r.status for r in results] == [
        LookupStatus.NOT_FOUND,
        LookupStatus.FOUND,
        LookupStatus.NOT_FOUND,
    ]


async def test_one_failing_item_does_not_poison_the_batch(
    build_resolver: Any, raw_track: dict[str, Any]
) -> None:
    client = FakeSearchClient(
        {"good": raw_track, "bad": SpotifyRateLimitError("rate limited", retry_after=3)},
        default=None,
    )
    resolver = build_resolver(client)

    results = await resolver.resolve(
        context=CONTEXT, items=[LookupItem(name="good"), LookupItem(name="bad")]
    )

    assert results[0].status is LookupStatus.FOUND
    assert results[1].status is LookupStatus.ERROR
    assert results[1].error is not None
    assert "rate limited" in results[1].error


@pytest.mark.parametrize(
    "failure",
    [
        SpotifyUnavailableError("upstream down"),
        SpotifyAuthError("bad credentials"),
        SpotifyRateLimitError("slow down"),
    ],
)
async def test_every_known_upstream_failure_becomes_an_error_result(
    build_resolver: Any, failure: Exception
) -> None:
    resolver = build_resolver(FakeSearchClient(default=failure))
    [result] = await resolver.resolve(context=CONTEXT, items=[LookupItem(name="x")])
    assert result.status is LookupStatus.ERROR
    assert result.error == str(failure)


async def test_an_unexpected_exception_is_contained_and_not_leaked_to_the_caller(
    build_resolver: Any,
) -> None:
    resolver = build_resolver(FakeSearchClient(default=RuntimeError("kaboom")))
    [result] = await resolver.resolve(context=CONTEXT, items=[LookupItem(name="x")])

    assert result.status is LookupStatus.ERROR
    # Internal failure detail must not reach the client verbatim.
    assert "kaboom" not in (result.error or "")
    assert result.error == "an unexpected error occurred while resolving this item"


async def test_concurrency_is_capped_at_the_configured_limit(build_resolver: Any) -> None:
    client = FakeSearchClient(default=None)
    client.delay = 0.02
    resolver = build_resolver(client, max_concurrency=3)

    await resolver.resolve(
        context=CONTEXT, items=[LookupItem(name=f"track {n}") for n in range(12)]
    )

    assert client.peak_in_flight <= 3
    assert len(client.seen) == 12


async def test_an_empty_batch_short_circuits_without_touching_the_upstream(
    build_resolver: Any,
) -> None:
    client = FakeSearchClient(default=None)
    resolver = build_resolver(client)
    assert await resolver.resolve(context=CONTEXT, items=[]) == []
    assert client.seen == []


async def test_the_request_market_wins_over_the_configured_default(
    build_resolver: Any,
) -> None:
    seen: list[str | None] = []

    class MarketSpy(FakeSearchClient):
        async def search_track(
            self, item: LookupItem, *, market: str | None, context: UserContext
        ) -> Any:
            seen.append(market)
            return None

    resolver = build_resolver(MarketSpy(), default_market="US")
    await resolver.resolve(context=CONTEXT, items=[LookupItem(name="x")], market="GB")
    await resolver.resolve(context=CONTEXT, items=[LookupItem(name="x")], default_market="US")
    await resolver.resolve(context=CONTEXT, items=[LookupItem(name="x")])

    assert seen == ["GB", "US", None]


async def test_health_is_delegated_to_the_client(build_resolver: Any) -> None:
    assert await build_resolver(FakeSearchClient(healthy=True)).check_health() is True
    assert await build_resolver(FakeSearchClient(healthy=False)).check_health() is False


@pytest.mark.parametrize(
    "failure",
    [
        UserTokenRejectedError("keyring refused the user token"),
        CredentialUnavailableError("not connected to Spotify"),
        KeyringUnavailableError("keyring is unreachable"),
    ],
)
async def test_a_credential_failure_fails_the_batch_rather_than_every_item(
    build_resolver: Any, failure: Exception
) -> None:
    # These are properties of the request, not of any item. Fifty identical
    # per-item errors would be a worse answer than one honest failure.
    resolver = build_resolver(FakeSearchClient(default=failure))
    with pytest.raises(type(failure)):
        await resolver.resolve([LookupItem(name="a"), LookupItem(name="b")], context=CONTEXT)
