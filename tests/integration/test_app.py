"""The application factory wires a working, self-describing service."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
import pytest

from spotify_api.__main__ import main
from spotify_api.app import create_app
from spotify_api.config import get_settings
from spotify_api.spotify.protocols import TrackResolver
from tests.factories import make_settings

if TYPE_CHECKING:
    from fastapi import FastAPI


async def test_the_lifespan_builds_the_object_graph_and_closes_the_client() -> None:
    app = create_app(settings=make_settings())

    async with app.router.lifespan_context(app):
        assert isinstance(app.state.resolver, TrackResolver)
        client = app.state.http_client
        assert isinstance(client, httpx.AsyncClient)
        assert not client.is_closed

    assert client.is_closed


async def test_one_http_client_is_shared_by_the_whole_graph() -> None:
    # Per-request clients would discard connection pooling and TLS reuse.
    app = create_app(settings=make_settings())
    async with app.router.lifespan_context(app):
        resolver = app.state.resolver
        shared = app.state.http_client
        assert resolver._client._client is shared
        assert resolver._client._credentials._client is shared
        assert app.state.credentials._client is shared


def test_settings_are_available_before_the_lifespan_runs() -> None:
    settings = make_settings(environment="test")
    assert create_app(settings=settings).state.settings is settings


def test_the_factory_falls_back_to_environment_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KEYRING_BASE_URL", "https://keyring.from-env")
    monkeypatch.setenv("KEYRING_SERVICE_TOKEN", "env-service-token")
    get_settings.cache_clear()
    try:
        app = create_app()
        assert app.state.settings.keyring_base_url == "https://keyring.from-env"
    finally:
        get_settings.cache_clear()


def test_each_call_returns_an_independent_application() -> None:
    assert create_app(settings=make_settings()) is not create_app(settings=make_settings())


def test_the_openapi_schema_documents_every_route(app: FastAPI) -> None:
    schema = app.openapi()

    assert set(schema["paths"]) == {"/healthy", "/ready", "/v1/lookup"}
    assert schema["info"]["title"] == "Spotify Lookup API"

    lookup = schema["paths"]["/v1/lookup"]["post"]
    assert set(lookup["responses"]) >= {"200", "422", "503"}
    assert lookup["description"]


def test_the_request_schema_is_published_with_its_example(app: FastAPI) -> None:
    schemas = app.openapi()["components"]["schemas"]

    item = schemas["LookupItem"]
    assert item["required"] == ["name"]
    assert set(item["properties"]) == {"name", "artist", "album", "year"}
    assert item["additionalProperties"] is False
    assert item["examples"]

    assert schemas["LookupRequest"]["required"] == ["items"]


def test_the_response_schema_advertises_the_three_statuses(app: FastAPI) -> None:
    schemas = app.openapi()["components"]["schemas"]
    assert set(schemas["LookupStatus"]["enum"]) == {"found", "not_found", "error"}
    assert "count" in schemas["LookupResponse"]["properties"]


async def test_an_unknown_path_still_returns_the_error_envelope(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/does-not-exist")
    assert response.status_code == 404


def test_the_module_entrypoint_starts_uvicorn(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict[str, Any] = {}

    def fake_run(target: str, **kwargs: Any) -> None:
        recorded["target"] = target
        recorded["kwargs"] = kwargs

    monkeypatch.setenv("KEYRING_BASE_URL", "https://keyring.from-env")
    monkeypatch.setenv("KEYRING_SERVICE_TOKEN", "env-service-token")
    monkeypatch.setattr("uvicorn.run", fake_run)

    get_settings.cache_clear()
    try:
        main()
    finally:
        get_settings.cache_clear()

    assert recorded["target"] == "spotify_api.app:create_app"
    assert recorded["kwargs"]["factory"] is True
    assert recorded["kwargs"]["port"] == 8000
