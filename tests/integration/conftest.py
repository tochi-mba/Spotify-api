"""Fixtures for route-level tests.

Routes are exercised against a fake resolver injected through the dependency overrides, so
these tests prove the HTTP contract without standing up an HTTP mock for Spotify. The adapter
itself is covered by its own unit tests.

Identity is not faked. Every request carries a token signed the way keyring signs them, and
the app verifies it against a JWKS document served by :mod:`keyring_client.testing` -- so a
route that forgot to scope by account, or a token this service should refuse, fails here
exactly as it would in production.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from keyring_client.testing import FakeKeyring

from spotify_api.api.dependencies import get_resolver, get_settings_dependency
from spotify_api.app import create_app
from spotify_api.models.responses import LookupResult, LookupStatus
from tests.factories import make_settings, user_token

if TYPE_CHECKING:
    import asyncio
    from collections.abc import AsyncIterator, Sequence

    from fastapi import FastAPI

    from spotify_api.credentials.models import UserContext
    from spotify_api.models.requests import LookupItem

ACCOUNT = "account-a"
OTHER_ACCOUNT = "account-b"

#: Every route needs one; individual tests override it to prove the failure paths.
USER_TOKEN = user_token(ACCOUNT)
OTHER_USER_TOKEN = user_token(OTHER_ACCOUNT)


class FakeResolver:
    """A TrackResolver that replays a scripted outcome per item name."""

    def __init__(self) -> None:
        self.script: dict[str, LookupResult] = {}
        self.raises: Exception | None = None
        self.healthy = True
        self.markets: list[str | None] = []
        self.batches: list[int] = []
        self.contexts: list[UserContext] = []
        self.gate: asyncio.Event | None = None

    async def resolve(
        self,
        items: Sequence[LookupItem],
        *,
        context: UserContext,
        market: str | None = None,
        default_market: str | None = None,
    ) -> list[LookupResult]:
        if self.gate is not None:
            await self.gate.wait()
        if self.raises is not None:
            raise self.raises
        self.contexts.append(context)
        self.markets.append(market if market is not None else default_market)
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
def keyring() -> FakeKeyring:
    """The keyring whose published keys every request's token is verified against."""
    return FakeKeyring()


@pytest.fixture
def settings_overrides() -> dict[str, Any]:
    return {}


@pytest.fixture
def app(
    resolver: FakeResolver, settings_overrides: dict[str, Any], keyring: FakeKeyring
) -> FastAPI:
    settings = make_settings(**settings_overrides)
    application = create_app(settings=settings, keyring_transport=keyring.transport())
    application.dependency_overrides[get_resolver] = lambda: resolver
    application.dependency_overrides[get_settings_dependency] = lambda: settings
    return application


def bearer(token: str) -> dict[str, str]:
    """Headers presenting ``token`` the canonical way."""
    return {"Authorization": f"Bearer {token}"}


@asynccontextmanager
async def client_for(app: FastAPI, token: str | None) -> AsyncIterator[httpx.AsyncClient]:
    """A client presenting ``token`` as a bearer credential, or none; keys are closed after."""
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    headers = bearer(token) if token is not None else {}
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", headers=headers
        ) as http_client:
            yield http_client
    finally:
        await app.state.jwks.aclose()
        await app.state.preferences.aclose()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """A client acting for ``account-a``."""
    async with client_for(app, USER_TOKEN) as http_client:
        yield http_client


@pytest.fixture
async def other_client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """A second person on the same deployment: ``account-b``."""
    async with client_for(app, OTHER_USER_TOKEN) as http_client:
        yield http_client


@pytest.fixture
async def anonymous_client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """A client that presents no keyring token."""
    async with client_for(app, None) as http_client:
        yield http_client
