"""Every failure is RFC 9457, and neither an exception's text nor the offending input leaks.

Driven against a throwaway app with one route per domain error, so the mapping is tested
directly rather than through whichever real route happens to raise each one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel, ConfigDict

from spotify_api import errors
from spotify_api.api.errors import (
    _DOMAIN_STATUS,
    PROBLEM_BASE_URI,
    WWW_AUTHENTICATE,
    _slug_for,
    problem_response,
    register_exception_handlers,
    unhandled_problem_response,
)
from spotify_api.api.middleware import REQUEST_ID_HEADER, RequestContextMiddleware
from spotify_api.context import bind_request_id
from spotify_api.errors import ConfirmationTimeoutError, ServiceError
from spotify_api.logging import configure_logging
from spotify_api.models.responses import PROBLEM_CONTENT_TYPE
from tests.conftest import log_records

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

SENTINEL = "sentinel-credential-9Q"


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")
    count: int


def throwaway() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)

    def route_raising(kind: type[ServiceError]) -> None:
        async def raiser() -> None:
            message = f"the detail for {kind.__name__}"
            raise kind(message)

        app.add_api_route(f"/domain/{kind.__name__}", raiser, methods=["GET"], name=kind.__name__)

    for error_type in _DOMAIN_STATUS:
        route_raising(error_type)

    @app.get("/observed")
    async def observed() -> None:
        message = "gave up"
        raise ConfirmationTimeoutError(message, observed={"is_playing": False, "context": None})

    @app.post("/validated")
    async def validated(body: Body) -> dict[str, int]:
        return {"count": body.count}

    @app.get("/unhandled")
    async def unhandled() -> None:
        message = f"failed https://user:{SENTINEL}@keyring.test/v1"
        raise RuntimeError(message)

    return app


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=throwaway()), base_url="http://t") as http:
        yield http


# -- the table ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error_type", "status"),
    list(_DOMAIN_STATUS.items()),
    ids=lambda value: getattr(value, "__name__", str(value)),
)
async def test_each_domain_error_maps_to_its_status_as_a_problem(
    client: AsyncClient, error_type: type[ServiceError], status: int
) -> None:
    response = await client.get(f"/domain/{error_type.__name__}")

    assert response.status_code == status
    assert response.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)
    problem = response.json()
    assert set(problem) == {"type", "title", "status", "detail", "instance", "request_id"}
    assert problem["type"] == f"{PROBLEM_BASE_URI}/{error_type.error_type.replace('_', '-')}"
    assert problem["status"] == status
    assert problem["detail"] == f"the detail for {error_type.__name__}"
    assert problem["instance"] == f"/domain/{error_type.__name__}"
    assert problem["request_id"] == response.headers[REQUEST_ID_HEADER]


def test_every_domain_error_is_mapped() -> None:
    # Anything absent would be rendered as an opaque 500, so a new failure has to be placed.
    defined = {
        obj
        for obj in vars(errors).values()
        if isinstance(obj, type) and issubclass(obj, ServiceError) and obj is not ServiceError
    }
    assert defined == set(_DOMAIN_STATUS)


def test_every_status_the_service_has_always_returned_is_kept() -> None:
    # Moving the status out of the exceptions and into this table changed no status code.
    assert {error.__name__: status for error, status in _DOMAIN_STATUS.items()} == {
        "BatchTooLargeError": 422,
        "ConfirmationTimeoutError": 504,
        "CredentialUnavailableError": 502,
        "JobNotFoundError": 404,
        "KeyringUnavailableError": 503,
        "NoActiveDeviceError": 409,
        "PreferencesUnavailableError": 503,
        "PremiumRequiredError": 403,
        "SpotifyAuthError": 503,
        "SpotifyRateLimitError": 503,
        "SpotifyUnavailableError": 503,
        "TrackLookupError": 502,
        "UserTokenRejectedError": 401,
    }


async def test_a_401_carries_a_bearer_challenge(client: AsyncClient) -> None:
    response = await client.get("/domain/UserTokenRejectedError")
    assert response.headers["WWW-Authenticate"] == WWW_AUTHENTICATE


async def test_structured_details_are_handed_back_with_their_nulls(client: AsyncClient) -> None:
    response = await client.get("/observed")

    assert response.status_code == 504
    # A null inside the observed state is information: nothing is loaded.
    assert response.json()["details"] == {"observed": {"is_playing": False, "context": None}}


# -- validation -----------------------------------------------------------------


async def test_the_offending_input_never_appears_in_a_validation_problem(
    client: AsyncClient,
) -> None:
    response = await client.post("/validated", json={"count": SENTINEL})

    assert response.status_code == 422
    assert SENTINEL not in response.text
    problem = response.json()
    assert problem["type"] == f"{PROBLEM_BASE_URI}/validation-failed"
    assert problem["detail"] == "the request failed validation"
    assert problem["errors"] == [
        {"location": "body.count", "message": problem["errors"][0]["message"]}
    ]


async def test_an_extra_field_is_named_by_location_and_not_echoed(client: AsyncClient) -> None:
    response = await client.post("/validated", json={"count": 1, "season": SENTINEL})

    assert response.status_code == 422
    assert response.json()["errors"][0]["location"] == "body.season"
    assert SENTINEL not in response.text


# -- everything else ------------------------------------------------------------


async def test_an_unknown_path_is_a_problem_too(client: AsyncClient) -> None:
    response = await client.get("/nowhere")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)
    assert response.json()["type"] == f"{PROBLEM_BASE_URI}/not-found"
    assert response.json()["instance"] == "/nowhere"


async def test_a_wrong_method_is_a_problem_that_still_says_what_is_allowed(
    client: AsyncClient,
) -> None:
    response = await client.get("/validated")

    assert response.status_code == 405
    assert response.headers["Allow"] == "POST"
    assert response.json()["type"] == f"{PROBLEM_BASE_URI}/method-not-allowed"


async def test_an_unhandled_exception_withholds_its_message(client: AsyncClient) -> None:
    response = await client.get("/unhandled")

    assert response.status_code == 500
    assert SENTINEL not in response.text
    problem = response.json()
    assert problem["type"] == f"{PROBLEM_BASE_URI}/internal-server-error"
    assert "quote the request id" in problem["detail"]
    assert problem["request_id"] == response.headers[REQUEST_ID_HEADER]


def test_a_status_nothing_maps_gets_a_generic_type_and_title() -> None:
    assert _slug_for(418) == "error"
    with bind_request_id("r-1"):
        response = problem_response(status_code=418, detail="d", instance="/teapot")
    assert b'"title":"Error"' in response.body
    assert b'"request_id":"r-1"' in response.body


def test_the_request_id_is_absent_outside_a_request() -> None:
    response = problem_response(status_code=400, detail="d", instance="/x")
    assert b"request_id" not in response.body


def test_unhandled_logs_the_type_and_the_stack_but_never_the_message(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(level="INFO", log_format="json")

    def explode() -> None:
        message = f"token was {SENTINEL}"
        raise ValueError(message)

    try:
        explode()
    except ValueError as exc:
        response = unhandled_problem_response(exc, instance="/x")

    out = capsys.readouterr().out
    assert response.status_code == 500
    [record] = [r for r in log_records(out) if r["event"] == "unhandled_exception"]
    assert record["error_type"] == "ValueError"
    # The stack is what makes it debuggable, and it is kept.
    assert "explode" in record["exception"]
    assert SENTINEL not in out
