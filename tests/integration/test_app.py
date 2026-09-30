"""The application factory wires a working, self-describing service."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from keyring_client import StdlibLogger
from settings_client import ResolvedSettings, Value

from spotify_api.__main__ import main
from spotify_api.api.dependencies import get_settings_dependency
from spotify_api.app import create_app
from spotify_api.config import get_settings
from spotify_api.models.responses import PROBLEM_CONTENT_TYPE
from spotify_api.preferences import DeploymentPreferences, build_preference_source
from spotify_api.spotify.protocols import TrackResolver
from tests.factories import TEST_SERVICE_TOKEN, make_settings, problem_type

if TYPE_CHECKING:
    from fastapi import FastAPI


async def test_the_lifespan_builds_the_object_graph_and_closes_the_client() -> None:
    app = create_app(settings=make_settings())

    async with app.router.lifespan_context(app):
        assert isinstance(app.state.resolver, TrackResolver)
        client = app.state.http_client
        assert isinstance(client, httpx.AsyncClient)
        assert not client.is_closed

    # The key cache is built with the app, so tests and probes that never run the lifespan can
    # still verify tokens; the lifespan is what closes its connections.
    assert app.state.jwks._client.is_closed
    assert client.is_closed


def test_without_settings_api_everybody_gets_the_configuration() -> None:
    app = create_app(settings=make_settings())
    assert isinstance(app.state.preferences, DeploymentPreferences)


async def test_a_settings_client_is_not_asked_at_startup_and_is_closed() -> None:
    class RecordingClient:
        def __init__(self) -> None:
            self.closed = False
            self.resolves = 0

        async def resolve(self, namespace: str, *, user_token: str) -> ResolvedSettings:
            self.resolves += 1
            message = "must not fetch settings at startup"
            raise AssertionError(message)

        async def set(self, namespace: str, key: str, value: Value, *, user_token: str) -> int:
            message = "must not write settings at startup"
            raise AssertionError(message)

        async def aclose(self) -> None:
            self.closed = True

    client = RecordingClient()
    settings = make_settings()
    app = create_app(
        settings=settings, preferences=build_preference_source(settings, client=client)
    )
    assert client.resolves == 0
    async with app.router.lifespan_context(app):
        assert client.resolves == 0
    assert client.closed


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


def test_the_settings_dependency_hands_out_the_apps_settings() -> None:
    settings = make_settings(environment="test")
    app = create_app(settings=settings)
    assert get_settings_dependency(SimpleNamespace(app=app)) is settings  # type: ignore[arg-type]


def test_the_factory_falls_back_to_environment_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SPOTIFY_API_KEYRING_BASE_URL", "https://keyring.from-env")
    monkeypatch.setenv("SPOTIFY_API_KEYRING_SERVICE_TOKEN", TEST_SERVICE_TOKEN)
    get_settings.cache_clear()
    try:
        app = create_app()
        assert app.state.settings.keyring_base_url == "https://keyring.from-env"
    finally:
        get_settings.cache_clear()


def test_each_call_returns_an_independent_application() -> None:
    assert create_app(settings=make_settings()) is not create_app(settings=make_settings())


def test_keyring_client_logs_through_this_services_logger() -> None:
    # Left to themselves the key cache and the verifier write to the standard library, bypassing
    # the format and the redactor every other record goes through.
    app = create_app(settings=make_settings())
    assert not isinstance(app.state.jwks._log, StdlibLogger)
    assert not isinstance(app.state.verifier._log, StdlibLogger)


def test_the_openapi_schema_documents_every_route(app: FastAPI) -> None:
    schema = app.openapi()

    paths = set(schema["paths"])
    assert {"/healthy", "/ready", "/v1/lookup", "/v1/jobs", "/v1/jobs/{job_id}"} <= paths

    # Every player endpoint is published, so a client can be generated from this.
    player = {p for p in paths if p.startswith("/v1/player")}
    assert player == {
        "/v1/player",
        "/v1/player/currently-playing",
        "/v1/player/devices",
        "/v1/player/next",
        "/v1/player/pause",
        "/v1/player/play",
        "/v1/player/previous",
        "/v1/player/queue",
        "/v1/player/recently-played",
        "/v1/player/repeat",
        "/v1/player/seek",
        "/v1/player/shuffle",
        "/v1/player/transfer",
        "/v1/player/volume",
    }
    assert schema["info"]["title"] == "Spotify Lookup API"

    lookup = schema["paths"]["/v1/lookup"]["post"]
    assert set(lookup["responses"]) >= {"200", "401", "422", "503"}
    assert lookup["description"]

    # The async flag is documented on the route, not just in prose.
    flags = [p for p in lookup["parameters"] if p["name"] == "async"]
    assert flags
    assert flags[0]["in"] == "query"

    # The token travels as a bearer credential, published as a security scheme so a generated
    # client sends it, and the header it used to travel in is still documented -- as deprecated.
    scheme = schema["components"]["securitySchemes"]["HTTPBearer"]
    assert (scheme["type"], scheme["scheme"]) == ("http", "bearer")
    assert "spotify-api" in scheme["description"]
    assert {"HTTPBearer": []} in lookup["security"]
    [legacy] = [p for p in lookup["parameters"] if p["name"] == "X-Keyring-User-Token"]
    assert legacy["in"] == "header"
    assert legacy["deprecated"] is True
    assert "security" not in schema["paths"]["/healthy"]["get"]

    # Every documented failure is a problem document.
    refused = lookup["responses"]["401"]["content"]["application/json"]["schema"]
    assert refused["$ref"] == "#/components/schemas/Problem"


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


async def test_an_unknown_path_is_a_problem_too(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/does-not-exist")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)
    assert response.json()["type"] == problem_type("not-found")


def test_the_module_entrypoint_starts_uvicorn(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict[str, Any] = {}

    def fake_run(target: str, **kwargs: Any) -> None:
        recorded["target"] = target
        recorded["kwargs"] = kwargs

    monkeypatch.setenv("SPOTIFY_API_KEYRING_BASE_URL", "https://keyring.from-env")
    monkeypatch.setenv("SPOTIFY_API_KEYRING_SERVICE_TOKEN", TEST_SERVICE_TOKEN)
    monkeypatch.setenv("SPOTIFY_API_HOST", "0.0.0.0")  # noqa: S104 - the point of the test
    monkeypatch.setenv("SPOTIFY_API_PORT", "8107")
    monkeypatch.setattr("uvicorn.run", fake_run)

    get_settings.cache_clear()
    try:
        main()
    finally:
        get_settings.cache_clear()

    assert recorded["target"] == "spotify_api.app:create_app"
    assert recorded["kwargs"]["factory"] is True
    # The bind is configuration, not a constant: the family assigns this service 8007 by
    # default, and a deployment may move it.
    assert recorded["kwargs"]["host"] == "0.0.0.0"  # noqa: S104 - what the env asked for
    assert recorded["kwargs"]["port"] == 8107
