"""Console entrypoint: ``python -m spotify_api``.

Uvicorn is handed the *factory* rather than an instantiated app, so the
application is built inside the worker process. That keeps the event loop, the
shared HTTP client and the connection pool owned by the process that actually
serves requests, which is what makes ``--workers`` safe.
"""

from __future__ import annotations

import uvicorn

from spotify_api.config import get_settings

__all__ = ["main"]


def main() -> None:
    """Start the ASGI server."""
    settings = get_settings()
    uvicorn.run(
        "spotify_api.app:create_app",
        factory=True,
        host="0.0.0.0",  # noqa: S104 -- containers must bind all interfaces
        port=8000,
        log_level=settings.log_level.lower(),
        access_log=False,  # our own middleware emits structured access logs
    )


if __name__ == "__main__":  # pragma: no cover
    main()
