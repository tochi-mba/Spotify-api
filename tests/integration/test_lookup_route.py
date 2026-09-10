"""The lookup endpoint's HTTP contract."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from spotify_api.errors import (
    CredentialUnavailableError,
    KeyringUnavailableError,
    SpotifyAuthError,
    SpotifyUnavailableError,
    UserTokenRejectedError,
)
from spotify_api.models.requests import LookupItem
from spotify_api.models.responses import Album, Artist, LookupResult, LookupStatus, Track

if TYPE_CHECKING:
    import httpx

    from tests.integration.conftest import FakeResolver

ENDPOINT = "/v1/lookup"


def found(name: str) -> LookupResult:
    return LookupResult(
        index=0,
        query=LookupItem(name=name),
        status=LookupStatus.FOUND,
        track=Track(
            id="7tFiyTwD0nx5a1eklYtX2J",
            name=name,
            artists=[Artist(id="1dfeR4HaWDbWqFHLkxsg1d", name="Queen")],
            album=Album(id="alb", name="A Night at the Opera", release_year=1975),
            duration_ms=354320,
            explicit=False,
            uri="spotify:track:7tFiyTwD0nx5a1eklYtX2J",
        ),
    )


async def test_a_found_track_comes_back_fully_populated(
    client: httpx.AsyncClient, resolver: FakeResolver
) -> None:
    resolver.script = {"Bohemian Rhapsody": found("Bohemian Rhapsody")}
    response = await client.post(
        ENDPOINT, json={"items": [{"name": "Bohemian Rhapsody", "artist": "Queen"}]}
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    [result] = payload["results"]
    assert result["index"] == 0
    assert result["status"] == "found"
    assert result["query"] == {
        "name": "Bohemian Rhapsody",
        "artist": "Queen",
        "album": None,
        "year": None,
    }
    assert result["track"]["artists"][0]["name"] == "Queen"
    assert result["error"] is None


async def test_the_response_carries_the_request_id(client: httpx.AsyncClient) -> None:
    response = await client.post(
        ENDPOINT, json={"items": [{"name": "x"}]}, headers={"X-Request-ID": "batch-7"}
    )
    assert response.json()["request_id"] == "batch-7"
    assert response.headers["X-Request-ID"] == "batch-7"


async def test_a_mixed_batch_returns_one_result_per_item_in_order(
    client: httpx.AsyncClient, resolver: FakeResolver
) -> None:
    resolver.script = {"hit": found("hit")}
    response = await client.post(
        ENDPOINT, json={"items": [{"name": "miss"}, {"name": "hit"}, {"name": "miss again"}]}
    )

    payload = response.json()
    assert payload["count"] == 3
    assert [r["index"] for r in payload["results"]] == [0, 1, 2]
    assert [r["status"] for r in payload["results"]] == ["not_found", "found", "not_found"]


async def test_a_failing_item_does_not_fail_the_request(
    client: httpx.AsyncClient, resolver: FakeResolver
) -> None:
    resolver.script = {
        "bad": LookupResult(
            index=0,
            query=LookupItem(name="bad"),
            status=LookupStatus.ERROR,
            error="Spotify rate limit exceeded",
        )
    }
    response = await client.post(ENDPOINT, json={"items": [{"name": "bad"}, {"name": "fine"}]})

    assert response.status_code == 200
    assert response.json()["results"][0]["error"] == "Spotify rate limit exceeded"


async def test_the_market_is_passed_through(
    client: httpx.AsyncClient, resolver: FakeResolver
) -> None:
    await client.post(ENDPOINT, json={"items": [{"name": "x"}], "market": "gb"})
    assert resolver.markets == ["GB"]


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"items": []},
        {"items": [{}]},
        {"items": [{"name": ""}]},
        {"items": [{"name": "x", "year": 1000}]},
        {"items": [{"name": "x", "season": 3}]},
        {"items": [{"name": "x"}], "market": "GBR"},
        {"items": "not a list"},
    ],
)
async def test_malformed_requests_are_rejected_with_the_error_envelope(
    client: httpx.AsyncClient, body: dict[str, Any]
) -> None:
    response = await client.post(ENDPOINT, json=body)

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["type"] == "validation_error"
    assert error["request_id"]
    assert error["details"]


@pytest.mark.parametrize("settings_overrides", [{"max_batch_size": 2}])
async def test_a_batch_over_the_configured_limit_is_rejected(
    client: httpx.AsyncClient, resolver: FakeResolver
) -> None:
    response = await client.post(ENDPOINT, json={"items": [{"name": f"t{n}"} for n in range(3)]})

    assert response.status_code == 422
    assert "at most 2" in response.json()["error"]["message"]
    assert resolver.batches == []


@pytest.mark.parametrize("settings_overrides", [{"max_batch_size": 2}])
async def test_a_batch_at_exactly_the_limit_is_accepted(client: httpx.AsyncClient) -> None:
    response = await client.post(ENDPOINT, json={"items": [{"name": "a"}, {"name": "b"}]})
    assert response.status_code == 200


async def test_an_upstream_outage_is_reported_as_service_unavailable(
    client: httpx.AsyncClient, resolver: FakeResolver
) -> None:
    resolver.raises = SpotifyUnavailableError("the Spotify API is unreachable")
    response = await client.post(ENDPOINT, json={"items": [{"name": "x"}]})

    assert response.status_code == 503
    error = response.json()["error"]
    assert error["type"] == "spotify_unavailable_error"
    assert error["message"] == "the Spotify API is unreachable"


async def test_bad_credentials_are_reported_as_service_unavailable_not_leaked(
    client: httpx.AsyncClient, resolver: FakeResolver
) -> None:
    resolver.raises = SpotifyAuthError("Spotify rejected the configured client credentials")
    response = await client.post(ENDPOINT, json={"items": [{"name": "x"}]})

    assert response.status_code == 503
    assert response.json()["error"]["type"] == "spotify_auth_error"


async def test_an_unexpected_failure_is_a_500_with_an_opaque_message(
    client: httpx.AsyncClient, resolver: FakeResolver
) -> None:
    resolver.raises = RuntimeError("connection pool corrupted")
    response = await client.post(ENDPOINT, json={"items": [{"name": "x"}]})

    assert response.status_code == 500
    error = response.json()["error"]
    assert error["type"] == "internal_error"
    assert "connection pool corrupted" not in response.text


async def test_a_get_is_not_allowed(client: httpx.AsyncClient) -> None:
    assert (await client.get(ENDPOINT)).status_code == 405


async def test_a_request_without_a_keyring_token_is_refused(
    anonymous_client: httpx.AsyncClient, resolver: FakeResolver
) -> None:
    response = await anonymous_client.post(ENDPOINT, json={"items": [{"name": "x"}]})

    assert response.status_code == 401
    error = response.json()["error"]
    assert error["type"] == "user_token_rejected"
    assert "X-Keyring-User-Token" in error["message"]
    assert resolver.batches == []


async def test_a_blank_keyring_token_is_refused(
    anonymous_client: httpx.AsyncClient,
) -> None:
    response = await anonymous_client.post(
        ENDPOINT, json={"items": [{"name": "x"}]}, headers={"X-Keyring-User-Token": "   "}
    )
    assert response.status_code == 401


async def test_the_user_token_reaches_the_resolver(
    client: httpx.AsyncClient, resolver: FakeResolver
) -> None:
    await client.post(ENDPOINT, json={"items": [{"name": "x"}]})
    assert resolver.contexts[0].user_token.get_secret_value() == "test-user-token"


async def test_the_profile_defaults_to_the_configured_one(
    client: httpx.AsyncClient, resolver: FakeResolver
) -> None:
    await client.post(ENDPOINT, json={"items": [{"name": "x"}]})
    assert resolver.contexts[0].profile == "personal"


async def test_a_caller_may_name_a_different_profile(
    client: httpx.AsyncClient, resolver: FakeResolver
) -> None:
    await client.post(
        ENDPOINT, json={"items": [{"name": "x"}]}, headers={"X-Keyring-Profile": "work"}
    )
    assert resolver.contexts[0].profile == "work"


async def test_a_blank_profile_header_falls_back_to_the_default(
    client: httpx.AsyncClient, resolver: FakeResolver
) -> None:
    await client.post(
        ENDPOINT, json={"items": [{"name": "x"}]}, headers={"X-Keyring-Profile": "  "}
    )
    assert resolver.contexts[0].profile == "personal"


async def test_a_refused_keyring_token_is_reported_as_401(
    client: httpx.AsyncClient, resolver: FakeResolver
) -> None:
    resolver.raises = UserTokenRejectedError("keyring refused the user token")
    response = await client.post(ENDPOINT, json={"items": [{"name": "x"}]})

    assert response.status_code == 401
    assert response.json()["error"]["type"] == "user_token_rejected"


async def test_a_missing_spotify_connection_is_reported_distinctly(
    client: httpx.AsyncClient, resolver: FakeResolver
) -> None:
    resolver.raises = CredentialUnavailableError("this profile is not connected to Spotify")
    response = await client.post(ENDPOINT, json={"items": [{"name": "x"}]})

    assert response.status_code == 502
    assert response.json()["error"]["type"] == "credential_unavailable"


async def test_keyring_being_down_is_reported_distinctly(
    client: httpx.AsyncClient, resolver: FakeResolver
) -> None:
    resolver.raises = KeyringUnavailableError("keyring is unreachable")
    response = await client.post(ENDPOINT, json={"items": [{"name": "x"}]})

    assert response.status_code == 503
    assert response.json()["error"]["type"] == "keyring_unavailable"
