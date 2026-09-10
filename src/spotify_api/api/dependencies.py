"""Dependency wiring.

Routes ask for a :class:`TrackResolver`, never for a Spotify client. The
concrete graph is assembled once during application startup and stashed on
``app.state``; these providers hand it out. Tests replace either provider
through FastAPI's ``dependency_overrides``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, Request

if TYPE_CHECKING:
    from spotify_api.config import Settings
    from spotify_api.spotify.protocols import TrackResolver

__all__ = [
    "ResolverDep",
    "SettingsDep",
    "get_resolver",
    "get_settings_dependency",
]


def get_resolver(request: Request) -> TrackResolver:
    """Return the resolver assembled at startup."""
    resolver: TrackResolver = request.app.state.resolver
    return resolver


def get_settings_dependency(request: Request) -> Settings:
    """Return the settings this application was created with."""
    settings: Settings = request.app.state.settings
    return settings


ResolverDep = Annotated["TrackResolver", Depends(get_resolver)]
SettingsDep = Annotated["Settings", Depends(get_settings_dependency)]
