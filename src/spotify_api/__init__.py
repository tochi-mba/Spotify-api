"""Spotify Lookup API.

A small, production-grade HTTP service that resolves a batch of loosely-specified
track descriptions against the Spotify Web API.

The public surface of this package is deliberately tiny: everything a consumer
needs is reachable through :func:`spotify_api.app.create_app`.
"""

from __future__ import annotations

__all__ = ["SERVICE_NAME", "__version__"]

#: Semantic version of the service. Kept in lockstep with ``pyproject.toml``.
__version__ = "0.1.0"

#: Stable identifier reported by ``GET /healthy``, and the audience keyring mints tokens for.
SERVICE_NAME = "spotify-api"
