"""Application factory.

``create_app`` is a factory rather than a module-level singleton so that tests
can build an isolated application per case, and so configuration is injected
rather than reached for.

The Spotify object graph is assembled once in the lifespan and shared for the
process's lifetime. In particular there is exactly one ``httpx.AsyncClient``:
building one per request would throw away connection pooling and TLS session
reuse, which is most of the cost of talking to Spotify at all.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import httpx
from fastapi import FastAPI

# Composition root: the JWKS client and token verifier are built here, once, for the process.
from keyring_client import JwksClient, SystemClock, TokenVerifier, jwks_url

import spotify_api
from spotify_api.api.errors import register_exception_handlers
from spotify_api.api.middleware import RequestContextMiddleware
from spotify_api.api.router import api_router, root_router
from spotify_api.config import Settings, get_settings
from spotify_api.credentials.keyring import KeyringCredentialProvider
from spotify_api.jobs.confirm import PlaybackConfirmer
from spotify_api.jobs.runner import JobRunner
from spotify_api.jobs.store import InMemoryJobStore
from spotify_api.logging import configure_logging, get_logger
from spotify_api.preferences import build_preference_source
from spotify_api.spotify.client import SpotifyClient
from spotify_api.spotify.resolver import SpotifyTrackResolver
from spotify_api.spotify.resources.player import PlayerResource

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from spotify_api.preferences import PreferenceSource

__all__ = ["create_app"]

_logger = get_logger(__name__)

_DESCRIPTION = """
Resolve batches of loosely-specified tracks against the Spotify Web API.

Submit an array of items shaped `{ name, artist?, album?, year? }` and receive
one result per item, in order, each carrying its own status. Only `name` is
required; the other fields are hints that narrow the search.

The service answers with **partial success** -- a track that cannot be resolved
comes back as a `not_found` or `error` result rather than failing the batch.
"""


def _build_graph(
    settings: Settings, client: httpx.AsyncClient
) -> tuple[SpotifyTrackResolver, KeyringCredentialProvider, SpotifyClient]:
    """Assemble the object graph over a shared HTTP client.

    One client serves both keyring and Spotify: they are different hosts, but
    httpx pools per host anyway, and a single client keeps shutdown to one
    close rather than two things to remember.
    """
    credentials = KeyringCredentialProvider(client=client, settings=settings)
    spotify_client = SpotifyClient(client=client, credentials=credentials, settings=settings)
    resolver = SpotifyTrackResolver(client=spotify_client, settings=settings)
    return resolver, credentials, spotify_client


def create_app(
    *,
    settings: Settings | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    keyring_transport: httpx.AsyncBaseTransport | None = None,
    preferences: PreferenceSource | None = None,
) -> FastAPI:
    """Build the application.

    Args:
        settings: Configuration to run with. Defaults to the process-wide
            settings loaded from the environment.
        transport: HTTP transport for the shared client. Left as ``None`` in
            production; tests pass an ``httpx.MockTransport`` to exercise the
            whole stack with the network as the only thing faked.
        keyring_transport: HTTP transport for fetching keyring's published keys. Defaults to
            ``transport``; tests that fake Spotify some other way pass keyring's fake here.
        preferences: Where per-person settings come from. Constructed here from
            ``settings`` when omitted; tests pass a source built over a fake client.
            The client is not asked until a request needs a person's settings.
    """
    resolved_settings = settings if settings is not None else get_settings()
    configure_logging(level=resolved_settings.log_level, log_format=resolved_settings.log_format)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Own the shared HTTP client and the object graph built over it."""
        limits = httpx.Limits(
            max_connections=resolved_settings.max_concurrency * 2,
            max_keepalive_connections=resolved_settings.max_concurrency,
        )
        async with httpx.AsyncClient(
            limits=limits,
            timeout=resolved_settings.request_timeout_seconds,
            transport=transport,
        ) as client:
            app.state.http_client = client
            resolver, credentials, spotify_client = _build_graph(resolved_settings, client)
            app.state.resolver = resolver
            app.state.credentials = credentials
            app.state.player = PlayerResource(client=spotify_client)
            app.state.confirmer = PlaybackConfirmer(
                client=spotify_client, settings=resolved_settings
            )
            app.state.job_store = InMemoryJobStore(ttl_seconds=resolved_settings.job_ttl_seconds)
            app.state.job_runner = JobRunner(store=app.state.job_store)
            _logger.info("application_started", environment=resolved_settings.environment)
            yield
            _logger.info("application_shutting_down")
            # Jobs outlive the request that created them, so they must be
            # cancelled deliberately or shutdown hangs on them.
            await app.state.job_runner.shutdown()
            await app.state.jwks.aclose()
            await app.state.preferences.aclose()

    app = FastAPI(
        title="Spotify Lookup API",
        description=_DESCRIPTION,
        version=spotify_api.__version__,
        lifespan=lifespan,
        openapi_tags=[
            {"name": "lookup", "description": "Batch track resolution."},
            {"name": "player", "description": "Playback control and inspection."},
            {"name": "jobs", "description": "Background jobs started with ?async=true."},
            {"name": "health", "description": "Liveness and readiness probes."},
        ],
    )

    # Available before the lifespan runs, so dependencies resolve in tests that
    # drive the app through an ASGI transport without a lifespan. The job store
    # holds no external resource, so it needs no lifespan of its own -- only
    # its runner does, to cancel work still in flight at shutdown.
    app.state.settings = resolved_settings
    app.state.preferences = (
        preferences if preferences is not None else build_preference_source(resolved_settings)
    )
    app.state.job_store = InMemoryJobStore(ttl_seconds=resolved_settings.job_ttl_seconds)
    app.state.job_runner = JobRunner(store=app.state.job_store)

    # Built with the app rather than in the lifespan, for the same reason as the job store:
    # a request must be verifiable before the lifespan has run. Constructing it makes no
    # network call -- the first token to arrive is what fetches keyring's keys -- so a keyring
    # that is down cannot stop this service starting. Both are handed this service's logger, so
    # why a token was refused reaches the operator in the same format, redacted by the same
    # rules, as every other record; left to themselves they would write to the standard library.
    clock = SystemClock()
    app.state.jwks = JwksClient(
        url=jwks_url(resolved_settings.keyring_base_url),
        clock=clock,
        cache_seconds=resolved_settings.jwks_cache_seconds,
        min_refetch_seconds=resolved_settings.jwks_min_refetch_seconds,
        timeout_seconds=resolved_settings.keyring_timeout_seconds,
        transport=keyring_transport if keyring_transport is not None else transport,
        logger=_logger,
    )
    app.state.verifier = TokenVerifier(
        jwks=app.state.jwks, issuer=resolved_settings.keyring_issuer, clock=clock, logger=_logger
    )

    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)
    app.include_router(root_router)
    app.include_router(api_router)
    return app
