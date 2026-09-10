"""Domain exceptions.

Each exception knows the HTTP status and machine-readable ``type`` it should be
rendered as, so route handlers never have to translate failures by hand and the
error envelope stays identical no matter where the failure originated.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "ServiceError",
    "SpotifyAuthError",
    "SpotifyRateLimitError",
    "SpotifyUnavailableError",
    "TrackLookupError",
]


class ServiceError(Exception):
    """Base class for every failure this service raises deliberately."""

    #: Stable, machine-readable discriminator returned to clients.
    error_type: str = "service_error"

    #: HTTP status this failure is rendered as at the boundary.
    status_code: int = 500

    def __init__(self, message: str, **details: Any) -> None:  # noqa: ANN401
        """Store ``message`` and any structured ``details`` for the envelope."""
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = {k: v for k, v in details.items() if v is not None}

    def to_payload(self, *, request_id: str) -> dict[str, Any]:
        """Render the public error envelope for this failure."""
        return {
            "error": {
                "type": self.error_type,
                "message": self.message,
                "details": self.details,
                "request_id": request_id,
            }
        }


class SpotifyAuthError(ServiceError):
    """Spotify rejected our credentials, or a token could not be obtained."""

    error_type = "spotify_auth_error"
    status_code = 503


class SpotifyRateLimitError(ServiceError):
    """Spotify returned 429 and the retry budget was exhausted."""

    error_type = "spotify_rate_limit_error"
    status_code = 503

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        """Record how long Spotify asked us to wait, when it told us."""
        super().__init__(message, retry_after=retry_after)
        self.retry_after = retry_after


class SpotifyUnavailableError(ServiceError):
    """Spotify was unreachable, timed out, or kept returning 5xx."""

    error_type = "spotify_unavailable_error"
    status_code = 503


class TrackLookupError(ServiceError):
    """A single item could not be resolved for a reason we can describe."""

    error_type = "track_lookup_error"
    status_code = 502
