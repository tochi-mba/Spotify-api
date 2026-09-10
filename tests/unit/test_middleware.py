"""Every response carries a request id, and every request is access-logged."""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING

import httpx
import pytest
from fastapi import FastAPI

from spotify_api.logging import JsonFormatter, current_request_id
from spotify_api.middleware import REQUEST_ID_HEADER, RequestContextMiddleware

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

UUID4_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


@pytest.fixture
def app() -> FastAPI:
    application = FastAPI()
    application.add_middleware(RequestContextMiddleware)

    @application.get("/echo")
    async def echo() -> dict[str, str]:
        return {"request_id": current_request_id()}

    @application.get("/boom")
    async def boom() -> dict[str, str]:
        message = "kaboom"
        raise RuntimeError(message)

    return application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
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


async def test_rejects_an_absurd_client_supplied_id_and_generates_its_own(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/echo", headers={REQUEST_ID_HEADER: "x" * 200})
    assert UUID4_RE.match(response.headers[REQUEST_ID_HEADER])


async def test_access_log_records_method_path_status_and_duration(
    client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="spotify_api.access"):
        await client.get("/echo")
    record = next(r for r in caplog.records if r.name == "spotify_api.access")
    assert record.http_method == "GET"  # type: ignore[attr-defined]
    assert record.http_path == "/echo"  # type: ignore[attr-defined]
    assert record.http_status == 200  # type: ignore[attr-defined]
    assert record.duration_ms >= 0  # type: ignore[attr-defined]


async def test_unhandled_errors_are_logged_with_their_request_id(
    client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.ERROR, logger="spotify_api.access"):
        response = await client.get("/boom", headers={REQUEST_ID_HEADER: "trace-me"})
    assert response.status_code == 500
    record = next(r for r in caplog.records if r.levelno == logging.ERROR)
    assert record.http_status == 500  # type: ignore[attr-defined]
    assert record.http_path == "/boom"  # type: ignore[attr-defined]
    assert json.loads(JsonFormatter().format(record))["request_id"] == "trace-me"


async def test_context_is_reset_between_requests(client: httpx.AsyncClient) -> None:
    first = await client.get("/echo", headers={REQUEST_ID_HEADER: "one"})
    second = await client.get("/echo")
    assert first.json()["request_id"] == "one"
    assert second.json()["request_id"] != "one"
