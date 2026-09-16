"""Every response carries a request id and a timing, and a crash still answers with the id."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import httpx
import pytest
from fastapi import FastAPI

from spotify_api.api.errors import register_exception_handlers
from spotify_api.api.middleware import (
    MAX_REQUEST_ID_LENGTH,
    REQUEST_ID_HEADER,
    RESPONSE_TIME_HEADER,
    RequestContextMiddleware,
)
from spotify_api.context import get_request_id
from spotify_api.logging import configure_logging
from tests.conftest import log_records

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

UUID4_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")

CRASH_TEXT = "kaboom https://user:password@upstream.test"


@pytest.fixture
def app() -> FastAPI:
    application = FastAPI()
    application.add_middleware(RequestContextMiddleware)
    register_exception_handlers(application)

    @application.get("/echo")
    async def echo() -> dict[str, str | None]:
        return {"request_id": get_request_id()}

    @application.get("/boom")
    async def boom() -> dict[str, str]:
        raise RuntimeError(CRASH_TEXT)

    return application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    configure_logging(level="INFO", log_format="json")
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client


async def test_generates_a_request_id_when_the_client_sends_none(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/echo")
    assert UUID4_RE.match(response.headers[REQUEST_ID_HEADER])
    assert response.json()["request_id"] == response.headers[REQUEST_ID_HEADER]


async def test_honours_a_client_supplied_request_id(client: httpx.AsyncClient) -> None:
    response = await client.get("/echo", headers={REQUEST_ID_HEADER: "caller-123"})
    assert response.headers[REQUEST_ID_HEADER] == "caller-123"
    assert response.json()["request_id"] == "caller-123"


async def test_an_id_at_exactly_the_limit_is_honoured(client: httpx.AsyncClient) -> None:
    supplied = "x" * MAX_REQUEST_ID_LENGTH
    response = await client.get("/echo", headers={REQUEST_ID_HEADER: supplied})
    assert response.headers[REQUEST_ID_HEADER] == supplied


async def test_rejects_an_absurd_client_supplied_id_and_generates_its_own(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get(
        "/echo", headers={REQUEST_ID_HEADER: "x" * (MAX_REQUEST_ID_LENGTH + 1)}
    )
    assert UUID4_RE.match(response.headers[REQUEST_ID_HEADER])


async def test_a_blank_client_supplied_id_is_replaced(client: httpx.AsyncClient) -> None:
    response = await client.get("/echo", headers={REQUEST_ID_HEADER: "   "})
    assert UUID4_RE.match(response.headers[REQUEST_ID_HEADER])


async def test_the_response_time_header_is_a_number_of_milliseconds(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/echo")
    assert float(response.headers[RESPONSE_TIME_HEADER]) >= 0


async def test_every_request_is_logged_with_method_path_status_and_duration(
    client: httpx.AsyncClient, capsys: pytest.CaptureFixture[str]
) -> None:
    response = await client.get("/echo")

    [record] = [
        r for r in log_records(capsys.readouterr().out) if r["event"] == "request_completed"
    ]
    assert (record["method"], record["path"], record["status_code"]) == ("GET", "/echo", 200)
    assert record["request_id"] == response.headers[REQUEST_ID_HEADER]
    assert record["duration_ms"] >= 0


async def test_a_crash_still_answers_with_a_problem_carrying_the_request_id(
    client: httpx.AsyncClient, capsys: pytest.CaptureFixture[str]
) -> None:
    response = await client.get("/boom", headers={REQUEST_ID_HEADER: "trace-me"})

    assert response.status_code == 500
    assert response.headers[REQUEST_ID_HEADER] == "trace-me"
    assert response.json()["request_id"] == "trace-me"
    assert response.json()["instance"] == "/boom"
    assert CRASH_TEXT not in response.text

    out = capsys.readouterr().out
    records = {record["event"]: record for record in log_records(out)}
    assert {"request_failed", "unhandled_exception", "request_completed"} <= set(records)
    assert records["unhandled_exception"]["request_id"] == "trace-me"
    assert records["request_completed"]["status_code"] == 500
    assert CRASH_TEXT not in out


async def test_context_is_reset_between_requests(client: httpx.AsyncClient) -> None:
    first = await client.get("/echo", headers={REQUEST_ID_HEADER: "one"})
    second = await client.get("/echo")

    assert first.json()["request_id"] == "one"
    assert second.json()["request_id"] != "one"
    assert get_request_id() is None
