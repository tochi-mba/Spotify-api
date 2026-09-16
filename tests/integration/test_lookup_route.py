"""The lookup endpoint's HTTP contract."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from keyring_client.testing import ISSUER, FakeKeyring, forge_hs256, mint

from spotify_api.api.dependencies import USER_TOKEN_HEADER
from spotify_api.api.errors import WWW_AUTHENTICATE
from spotify_api.errors import (
    CredentialUnavailableError,
    KeyringUnavailableError,
    SpotifyAuthError,
    SpotifyUnavailableError,
    UserTokenRejectedError,
)
from spotify_api.models.requests import LookupItem
from spotify_api.models.responses import (
    PROBLEM_CONTENT_TYPE,
    Album,
    Artist,
    LookupResult,
    LookupStatus,
    Track,
)
from tests.conftest import log_records
from tests.factories import AUDIENCE, problem_type
from tests.integration.conftest import ACCOUNT, OTHER_USER_TOKEN, USER_TOKEN, bearer

if TYPE_CHECKING:
    from tests.integration.conftest import FakeResolver

ENDPOINT = "/v1/lookup"

#: What every refused token is told, whichever rule refused it.
TOKEN_NOT_ACCEPTED = "the keyring user token was not accepted"


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
async def test_malformed_requests_are_rejected_as_a_problem(
    client: httpx.AsyncClient, body: dict[str, Any]
) -> None:
    response = await client.post(ENDPOINT, json=body)

    assert response.status_code == 422
    assert response.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)
    problem = response.json()
    assert problem["type"] == problem_type("validation-failed")
    assert problem["instance"] == ENDPOINT
    assert problem["request_id"] == response.headers["X-Request-ID"]
    assert problem["errors"]


async def test_a_rejected_value_is_named_by_location_and_never_echoed(
    client: httpx.AsyncClient,
) -> None:
    # FastAPI's own handler would put the offending input into the response.
    response = await client.post(ENDPOINT, json={"items": [{"name": "x", "season": "echo-me-not"}]})

    assert response.status_code == 422
    assert "echo-me-not" not in response.text
    assert response.json()["errors"][0]["location"] == "body.items.0.season"


@pytest.mark.parametrize("settings_overrides", [{"max_batch_size": 2}])
async def test_a_batch_over_the_configured_limit_is_rejected(
    client: httpx.AsyncClient, resolver: FakeResolver
) -> None:
    response = await client.post(ENDPOINT, json={"items": [{"name": f"t{n}"} for n in range(3)]})

    assert response.status_code == 422
    problem = response.json()
    assert problem["type"] == problem_type("batch-too-large")
    assert "at most 2" in problem["detail"]
    assert problem["details"] == {"limit": 2, "received": 3}
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
    problem = response.json()
    assert problem["type"] == problem_type("spotify-unavailable-error")
    assert problem["detail"] == "the Spotify API is unreachable"


async def test_bad_credentials_are_reported_as_service_unavailable_not_leaked(
    client: httpx.AsyncClient, resolver: FakeResolver
) -> None:
    resolver.raises = SpotifyAuthError("Spotify rejected the configured client credentials")
    response = await client.post(ENDPOINT, json={"items": [{"name": "x"}]})

    assert response.status_code == 503
    assert response.json()["type"] == problem_type("spotify-auth-error")


async def test_an_unexpected_failure_is_a_500_with_an_opaque_message(
    client: httpx.AsyncClient, resolver: FakeResolver
) -> None:
    resolver.raises = RuntimeError("connection pool corrupted")
    response = await client.post(ENDPOINT, json={"items": [{"name": "x"}]})

    assert response.status_code == 500
    problem = response.json()
    assert problem["type"] == problem_type("internal-server-error")
    assert problem["request_id"] == response.headers["X-Request-ID"]
    assert "connection pool corrupted" not in response.text


async def test_a_get_is_not_allowed(client: httpx.AsyncClient) -> None:
    response = await client.get(ENDPOINT)

    assert response.status_code == 405
    assert response.headers["Allow"] == "POST"
    assert response.json()["type"] == problem_type("method-not-allowed")


async def test_a_request_without_a_keyring_token_is_refused(
    anonymous_client: httpx.AsyncClient, resolver: FakeResolver
) -> None:
    response = await anonymous_client.post(ENDPOINT, json={"items": [{"name": "x"}]})

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == WWW_AUTHENTICATE
    problem = response.json()
    assert problem["type"] == problem_type("user-token-rejected")
    assert "Authorization: Bearer" in problem["detail"]
    assert resolver.batches == []


async def test_a_blank_keyring_token_is_refused(
    anonymous_client: httpx.AsyncClient,
) -> None:
    response = await anonymous_client.post(
        ENDPOINT, json={"items": [{"name": "x"}]}, headers={USER_TOKEN_HEADER: "   "}
    )
    assert response.status_code == 401


async def test_the_user_token_reaches_the_resolver(
    client: httpx.AsyncClient, resolver: FakeResolver
) -> None:
    await client.post(ENDPOINT, json={"items": [{"name": "x"}]})
    assert resolver.contexts[0].user_token.get_secret_value() == USER_TOKEN


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
    assert response.json()["type"] == problem_type("user-token-rejected")


async def test_a_missing_spotify_connection_is_reported_distinctly(
    client: httpx.AsyncClient, resolver: FakeResolver
) -> None:
    resolver.raises = CredentialUnavailableError("this profile is not connected to Spotify")
    response = await client.post(ENDPOINT, json={"items": [{"name": "x"}]})

    assert response.status_code == 502
    assert response.json()["type"] == problem_type("credential-unavailable")


async def test_keyring_being_down_is_reported_distinctly(
    client: httpx.AsyncClient, resolver: FakeResolver
) -> None:
    resolver.raises = KeyringUnavailableError("keyring is unreachable")
    response = await client.post(ENDPOINT, json={"items": [{"name": "x"}]})

    assert response.status_code == 503
    assert response.json()["type"] == problem_type("keyring-unavailable")


# -- the token is verified here, not merely forwarded --------------------------


async def test_the_verified_account_reaches_the_resolver(
    client: httpx.AsyncClient, resolver: FakeResolver
) -> None:
    await client.post(ENDPOINT, json={"items": [{"name": "x"}]})
    assert resolver.contexts[0].account_id == ACCOUNT


@pytest.mark.parametrize(
    "token",
    [
        pytest.param(
            mint(account_id=ACCOUNT, audience="web-search-api", issuer=ISSUER),
            id="another-services-token",
        ),
        pytest.param(
            mint(account_id=ACCOUNT, audience=AUDIENCE, issuer="https://another-keyring.test"),
            id="another-issuer",
        ),
        pytest.param(
            mint(account_id=ACCOUNT, audience=AUDIENCE, issuer=ISSUER, ttl_seconds=-60),
            id="expired",
        ),
        pytest.param(
            forge_hs256(account_id=ACCOUNT, audience=AUDIENCE, issuer=ISSUER),
            id="hs256-signed-with-the-public-key",
        ),
        pytest.param("not-a-token", id="malformed"),
    ],
)
async def test_a_token_keyring_did_not_mint_for_this_service_is_refused_before_any_work(
    anonymous_client: httpx.AsyncClient, resolver: FakeResolver, token: str
) -> None:
    response = await anonymous_client.post(
        ENDPOINT, json={"items": [{"name": "x"}]}, headers=bearer(token)
    )

    assert response.status_code == 401
    problem = response.json()
    assert problem["type"] == problem_type("user-token-rejected")
    # One message whichever rule refused it: a forger learns nothing from the difference.
    assert problem["detail"] == TOKEN_NOT_ACCEPTED
    assert resolver.batches == []


async def test_keyrings_keys_being_unreachable_is_a_503_not_a_401(
    client: httpx.AsyncClient, keyring: FakeKeyring, resolver: FakeResolver
) -> None:
    keyring.error = httpx.ConnectError("keyring is down")

    response = await client.post(ENDPOINT, json={"items": [{"name": "x"}]})

    # The token may be perfectly good; telling the caller to sign in again would not help.
    assert response.status_code == 503
    assert response.json()["type"] == problem_type("keyring-unavailable")
    assert resolver.batches == []


# -- where the token travels --------------------------------------------------


async def test_the_legacy_header_alone_is_still_accepted_and_logged(
    anonymous_client: httpx.AsyncClient,
    resolver: FakeResolver,
    capsys: pytest.CaptureFixture[str],
) -> None:
    response = await anonymous_client.post(
        ENDPOINT, json={"items": [{"name": "x"}]}, headers={USER_TOKEN_HEADER: USER_TOKEN}
    )

    assert response.status_code == 200
    assert resolver.contexts[0].account_id == ACCOUNT
    # Logged, so the callers still sending it can be found before it stops being accepted.
    [record] = [
        r for r in log_records(capsys.readouterr().out) if r["event"] == "legacy_user_token_header"
    ]
    assert record["replacement"] == "Authorization: Bearer"
    assert record["request_id"] == response.headers["X-Request-ID"]
    assert USER_TOKEN not in json.dumps(record)


async def test_a_bearer_token_is_not_logged_as_the_legacy_header(
    client: httpx.AsyncClient, capsys: pytest.CaptureFixture[str]
) -> None:
    response = await client.post(ENDPOINT, json={"items": [{"name": "x"}]})

    assert response.status_code == 200
    events = [r["event"] for r in log_records(capsys.readouterr().out)]
    assert "request_completed" in events
    assert "legacy_user_token_header" not in events


async def test_the_bearer_scheme_is_matched_case_insensitively(
    anonymous_client: httpx.AsyncClient, resolver: FakeResolver
) -> None:
    response = await anonymous_client.post(
        ENDPOINT, json={"items": [{"name": "x"}]}, headers={"Authorization": f"bearer {USER_TOKEN}"}
    )

    assert response.status_code == 200
    assert resolver.contexts[0].account_id == ACCOUNT


async def test_both_headers_carrying_the_same_token_are_accepted(
    anonymous_client: httpx.AsyncClient, resolver: FakeResolver
) -> None:
    response = await anonymous_client.post(
        ENDPOINT,
        json={"items": [{"name": "x"}]},
        headers={**bearer(USER_TOKEN), USER_TOKEN_HEADER: USER_TOKEN},
    )

    assert response.status_code == 200
    assert resolver.contexts[0].account_id == ACCOUNT


async def test_both_headers_carrying_different_tokens_are_refused(
    anonymous_client: httpx.AsyncClient, resolver: FakeResolver
) -> None:
    # Both are good tokens for this service, belonging to two different people. Believing either
    # one would be choosing whose account to act on at the caller's say-so.
    response = await anonymous_client.post(
        ENDPOINT,
        json={"items": [{"name": "x"}]},
        headers={**bearer(USER_TOKEN), USER_TOKEN_HEADER: OTHER_USER_TOKEN},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == TOKEN_NOT_ACCEPTED
    assert resolver.batches == []


@pytest.mark.parametrize(
    "authorization",
    [
        pytest.param(f"Basic {USER_TOKEN}", id="basic"),
        pytest.param(USER_TOKEN, id="no-scheme"),
        pytest.param("Bearer", id="bearer-without-a-token"),
        pytest.param("", id="empty"),
    ],
)
@pytest.mark.parametrize("with_legacy", [False, True], ids=["alone", "beside-a-good-legacy-header"])
async def test_an_authorization_header_that_is_not_a_bearer_credential_is_refused(
    anonymous_client: httpx.AsyncClient,
    resolver: FakeResolver,
    authorization: str,
    with_legacy: bool,
) -> None:
    # A header that is present was meant. A good legacy header beside it is not a fallback, or the
    # caller could never tell which of the two was believed.
    headers = {"Authorization": authorization}
    if with_legacy:
        headers[USER_TOKEN_HEADER] = USER_TOKEN

    response = await anonymous_client.post(
        ENDPOINT, json={"items": [{"name": "x"}]}, headers=headers
    )

    assert response.status_code == 401
    assert response.json()["detail"] == TOKEN_NOT_ACCEPTED
    assert resolver.batches == []
