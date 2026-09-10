"""Router aggregation.

Health endpoints sit at the root because probes should not have to track an API
version. Everything else is versioned, so the contract can evolve without
breaking existing callers.
"""

from __future__ import annotations

from fastapi import APIRouter

from spotify_api.api.routes import health, jobs, lookup

__all__ = ["api_router", "root_router"]

root_router = APIRouter()
root_router.include_router(health.router)

api_router = APIRouter(prefix="/v1")
api_router.include_router(lookup.router)
api_router.include_router(jobs.router)
