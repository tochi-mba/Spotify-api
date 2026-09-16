"""Health endpoints.

Liveness and readiness are deliberately separate. ``/healthy`` answers from
process state alone, so an orchestrator never restarts a perfectly healthy
container because Spotify is having a bad afternoon. ``/ready`` is the one that
consults the upstream, and is what a load balancer should gate traffic on.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Response, status

import spotify_api
from spotify_api.api.dependencies import ResolverDep
from spotify_api.models.responses import HealthResponse, ReadyResponse

__all__ = ["router"]

router = APIRouter(tags=["health"])

#: Captured at import so uptime is measured from process start.
_STARTED_AT = time.monotonic()


@router.get(
    "/healthy",
    response_model=HealthResponse,
    summary="Liveness probe",
    description=(
        "Reports that the process is alive and serving. Makes no outbound "
        "calls, so it can never be failed by a third party."
    ),
)
async def healthy() -> HealthResponse:
    """Answer the liveness probe."""
    return HealthResponse(
        version=spotify_api.__version__,
        uptime_seconds=round(time.monotonic() - _STARTED_AT, 3),
    )


@router.get(
    "/ready",
    response_model=ReadyResponse,
    summary="Readiness probe",
    description=(
        "Reports whether the service can currently serve lookups, which means "
        "keyring's published signing keys can be fetched. Returns 503 when they "
        "cannot. The JSON still names that check `spotify`: it is the gate on "
        "whether this process can talk to Spotify on anyone's behalf."
    ),
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReadyResponse}},
)
async def ready(resolver: ResolverDep, response: Response) -> ReadyResponse:
    """Answer the readiness probe."""
    spotify_ok = await resolver.check_health()
    if not spotify_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadyResponse.from_spotify_health(spotify_ok=spotify_ok)
