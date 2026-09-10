"""Fixtures for route-level tests.

Routes are exercised against a fake resolver injected through the dependency
overrides, so these tests prove the HTTP contract without standing up an HTTP
mock for Spotify. The adapter itself is covered by its own unit tests.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
import pytest

from spotify_api.api.dependencies import get_resolver, get_settings_dependency
from spotify_api.app import create_app
from spotify_api.models.responses import LookupResult, LookupStatus
from tests.factories import make_settings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    from fastapi import FastAPI

    from spotify_api.models.requests import LookupItem


class FakeResolver:
    """A TrackResolver that replays a scripted outcome per item name."""

    def __init__(self) -> None:
        self.script: dict[str, LookupResult] = {}
        self.raises: Exception | None = None
        self.healthy = True
        self.markets: list[str | None] = []
        self.batches: list[int] = []

    async def resolve(
        self, items: Sequence[LookupItem], *, market: str | None = None
    ) -> list[LookupResult]:
        if self.raises is not None:
            raise self.raises
        self.markets.append(market)
        self.batches.append(len(items))
        results = []
        for index, item in enumerate(items):
            scripted = self.script.get(item.name)
            if scripted is not None:
                results.append(scripted.model_copy(update={"index": index, "query": item}))
            else:
                results.append(LookupResult(index=index, query=item, status=LookupStatus.NOT_FOUND))
        return results

    async def check_health(self) -> bool:
        return self.healthy


@pytest.fixture
def resolver() -> FakeResolver:
    return FakeResolver()


@pytest.fixture
def settings_overrides() -> dict[str, Any]:
    return {}


@pytest.fixture
def app(resolver: FakeResolver, settings_overrides: dict[str, Any]) -> FastAPI:
    settings = make_settings(**settings_overrides)
    application = create_app(settings=settings)
    application.dependency_overrides[get_resolver] = lambda: resolver
    application.dependency_overrides[get_settings_dependency] = lambda: settings
    return application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client
