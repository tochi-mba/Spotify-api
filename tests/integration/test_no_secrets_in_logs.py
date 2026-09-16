"""Neither the caller's token nor this service's own credentials reach a log record, on any path.

The whole stack runs -- token verification, the keyring credential provider, the Spotify client,
the job runner -- with the network as the only thing faked and logging at DEBUG in JSON. Every
captured record is parsed and searched recursively, and the raw output is searched as well, so an
occurrence can hide neither inside a structure nor outside a record. Each test also asserts that
records were captured at all: a change that silenced logging must not pass by proving nothing.

The failures are as unkind as they come. The upstreams raise exceptions whose *messages* carry
the tokens, because that is exactly how a credential ends up in a traceback.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from keyring_client import JWKS_PATH
from keyring_client.testing import ISSUER, jwks, mint

from spotify_api.api.dependencies import USER_TOKEN_HEADER
from spotify_api.app import create_app
from tests.conftest import log_records
from tests.factories import TEST_SERVICE_TOKEN, make_settings, user_token

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

KEYRING_URL = "https://keyring.test"
SPOTIFY_URL = "https://api.spotify.test/v1"

USER_TOKEN = user_token("log-account")
FOREIGN_TOKEN = mint(account_id="log-account", audience="web-search-api", issuer=ISSUER)
SPOTIFY_ACCESS_TOKEN = "spotify-access-token-sentinel-0123456789"

SECRETS = (USER_TOKEN, FOREIGN_TOKEN, TEST_SERVICE_TOKEN, SPOTIFY_ACCESS_TOKEN)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def leaky(*secrets: str) -> str:
    """An exception message of the kind an HTTP client writes: a URL, credentials and all."""
    return "failed https://" + ":".join(secrets) + "@upstream.test/v1"


def mentions(record: object, needle: str) -> bool:
    if isinstance(record, str):
        return needle in record
    if isinstance(record, dict):
        return any(mentions(k, needle) or mentions(v, needle) for k, v in record.items())
    if isinstance(record, list):
        return any(mentions(item, needle) for item in record)
    return False


def assert_no_secrets(out: str) -> list[dict[str, Any]]:
    records = log_records(out)
    assert records, "no log records were captured; the test proves nothing"
    for secret in SECRETS:
        assert secret not in out
        assert not [record for record in records if mentions(record, secret)]
    return records


class Upstreams:
    """keyring and Spotify, each able to fail in the ways that put a token in an exception."""

    def __init__(self) -> None:
        self.keyring_status = 200
        self.keyring_error: Exception | None = None
        self.spotify_error: Exception | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "keyring.test":
            return self._keyring(request)
        if self.spotify_error is not None:
            raise self.spotify_error
        if request.url.path.endswith("/me/player"):
            return httpx.Response(204)
        return httpx.Response(200, json={"tracks": {"items": []}})

    def _keyring(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == JWKS_PATH:
            return httpx.Response(200, json=jwks())
        if self.keyring_error is not None:
            raise self.keyring_error
        if self.keyring_status != 200:
            return httpx.Response(self.keyring_status, json={"detail": "no"})
        return httpx.Response(
            200,
            json={
                "service": "spotify",
                "headers": {"Authorization": f"Bearer {SPOTIFY_ACCESS_TOKEN}"},
                "query_params": {},
                "expires_at": None,
            },
        )


@pytest.fixture
def upstreams() -> Upstreams:
    return Upstreams()


@pytest.fixture
async def client(upstreams: Upstreams) -> AsyncIterator[httpx.AsyncClient]:
    settings = make_settings(
        keyring_base_url=KEYRING_URL,
        spotify_base_url=SPOTIFY_URL,
        log_level="DEBUG",
        max_retries=0,
        confirm_timeout_seconds=0.01,
        confirm_poll_interval_seconds=0.001,
    )
    app = create_app(settings=settings, transport=httpx.MockTransport(upstreams))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
            yield http_client


async def poll_until_finished(client: httpx.AsyncClient, poll_url: str) -> dict[str, Any]:
    for _ in range(100):
        await asyncio.sleep(0)
        job: dict[str, Any] = (await client.get(poll_url, headers=bearer(USER_TOKEN))).json()
        if job["status"] not in ("pending", "running"):
            return job
    pytest.fail("the job never finished")


async def test_no_secret_is_logged_on_success(
    client: httpx.AsyncClient, capsys: pytest.CaptureFixture[str]
) -> None:
    lookup = await client.post(
        "/v1/lookup", json={"items": [{"name": "x"}]}, headers=bearer(USER_TOKEN)
    )
    player = await client.get("/v1/player", headers=bearer(USER_TOKEN))
    accepted = await client.post(
        "/v1/lookup?async=true", json={"items": [{"name": "x"}]}, headers=bearer(USER_TOKEN)
    )
    job = await poll_until_finished(client, accepted.json()["poll_url"])

    assert (lookup.status_code, player.status_code, job["status"]) == (200, 200, "succeeded")
    assert_no_secrets(capsys.readouterr().out)


async def test_no_secret_is_logged_when_a_token_is_refused_or_arrives_the_old_way(
    client: httpx.AsyncClient, capsys: pytest.CaptureFixture[str]
) -> None:
    body = {"items": [{"name": "x"}]}
    foreign = await client.post("/v1/lookup", json=body, headers=bearer(FOREIGN_TOKEN))
    mismatch = await client.post(
        "/v1/lookup", json=body, headers={**bearer(USER_TOKEN), USER_TOKEN_HEADER: FOREIGN_TOKEN}
    )
    legacy = await client.post("/v1/lookup", json=body, headers={USER_TOKEN_HEADER: USER_TOKEN})

    assert (foreign.status_code, mismatch.status_code, legacy.status_code) == (401, 401, 200)
    records = assert_no_secrets(capsys.readouterr().out)
    # keyring-client's own reason for the refusal reaches this service's log, in its format.
    assert {"token_rejected", "legacy_user_token_header"} <= {r["event"] for r in records}


@pytest.mark.parametrize("keyring_status", [401, 404, 500, 503])
async def test_no_secret_is_logged_when_keyring_refuses_or_fails(
    client: httpx.AsyncClient,
    upstreams: Upstreams,
    capsys: pytest.CaptureFixture[str],
    keyring_status: int,
) -> None:
    upstreams.keyring_status = keyring_status

    response = await client.post(
        "/v1/lookup", json={"items": [{"name": "x"}]}, headers=bearer(USER_TOKEN)
    )

    assert response.status_code >= 400
    assert_no_secrets(capsys.readouterr().out)


async def test_no_secret_is_logged_or_answered_when_an_upstream_is_unreachable(
    client: httpx.AsyncClient, upstreams: Upstreams, capsys: pytest.CaptureFixture[str]
) -> None:
    upstreams.keyring_error = httpx.ConnectError(leaky(USER_TOKEN, TEST_SERVICE_TOKEN))
    keyring_down = await client.post(
        "/v1/lookup", json={"items": [{"name": "x"}]}, headers=bearer(USER_TOKEN)
    )
    upstreams.keyring_error = None
    upstreams.spotify_error = httpx.ConnectError(leaky(SPOTIFY_ACCESS_TOKEN))
    spotify_down = await client.get("/v1/player", headers=bearer(USER_TOKEN))

    assert (keyring_down.status_code, spotify_down.status_code) == (503, 503)
    for secret in SECRETS:
        assert secret not in keyring_down.text
        assert secret not in spotify_down.text
    assert_no_secrets(capsys.readouterr().out)


async def test_no_secret_is_logged_when_an_unanticipated_exception_carries_one(
    client: httpx.AsyncClient, upstreams: Upstreams, capsys: pytest.CaptureFixture[str]
) -> None:
    # Nothing below the route catches a non-HTTP exception: within a batch it becomes one error
    # result, on a player route a 500, and in a job a failed job. Each of those logs the stack.
    upstreams.spotify_error = RuntimeError(
        leaky(USER_TOKEN, TEST_SERVICE_TOKEN, SPOTIFY_ACCESS_TOKEN)
    )

    item = await client.post(
        "/v1/lookup", json={"items": [{"name": "x"}]}, headers=bearer(USER_TOKEN)
    )
    route = await client.get("/v1/player", headers=bearer(USER_TOKEN))
    accepted = await client.post("/v1/player/pause?async=true", headers=bearer(USER_TOKEN))
    job = await poll_until_finished(client, accepted.json()["poll_url"])

    assert item.json()["results"][0]["status"] == "error"
    assert route.status_code == 500
    assert job["status"] == "failed"
    records = assert_no_secrets(capsys.readouterr().out)
    assert {
        "item_lookup_failed_unexpectedly",
        "unhandled_exception",
        "job_failed_unexpectedly",
    } <= {r["event"] for r in records}
