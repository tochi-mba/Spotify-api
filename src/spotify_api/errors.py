"""Domain exceptions.

Each exception knows the HTTP status and machine-readable ``type`` it should be
rendered as, so route handlers never have to translate failures by hand and the
error envelope stays identical no matter where the failure originated.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from spotify_api.logging import current_request_id

if TYPE_CHECKING:
    from starlette.responses import Response

__all__ = [
    "BatchTooLargeError",
    "CredentialUnavailableError",
    "KeyringUnavailableError",
    "NoActiveDeviceError",
    "PremiumRequiredError",
    "ServiceError",
    "SpotifyAuthError",
    "SpotifyRateLimitError",
    "SpotifyUnavailableError",
    "TrackLookupError",
    "UserTokenRejectedError",
    "install_exception_handlers",
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


class BatchTooLargeError(ServiceError):
    """The batch exceeded this deployment's configured operational limit."""

    error_type = "batch_too_large"
    status_code = 422


class UserTokenRejectedError(ServiceError):
    """The caller's keyring user token is missing, expired, or not for us.

    A 401 rather than a 503: the caller can fix this by presenting a valid
    token, which is not true of the other credential failures.
    """

    error_type = "user_token_rejected"
    status_code = 401


class CredentialUnavailableError(ServiceError):
    """keyring has no usable Spotify credential for this user and profile.

    The grant was revoked, never made, or could not be refreshed. Retrying
    cannot help -- the user must reconnect Spotify in keyring.
    """

    error_type = "credential_unavailable"
    status_code = 502


class KeyringUnavailableError(ServiceError):
    """keyring itself is unreachable or failing.

    Distinct from CredentialUnavailableError on purpose: this one is worth
    retrying, and it points at an operator problem rather than a user one.
    """

    error_type = "keyring_unavailable"
    status_code = 503


class SpotifyAuthError(ServiceError):
    """Spotify rejected our credentials, or a token could not be obtained."""

    error_type = "spotify_auth_error"
    status_code = 503


class PremiumRequiredError(ServiceError):
    """The action needs Spotify Premium and this account does not have it.

    Named separately because it is the single most common reason playback
    fails, and "upgrade your account" is a useful thing to be told.
    """

    error_type = "premium_required"
    status_code = 403


class NoActiveDeviceError(ServiceError):
    """Spotify has no device to act on.

    Playback commands need somewhere to play. Spotify reports this as a bare
    404, which is indistinguishable from a missing resource unless you look at
    the route -- so it is translated here into something actionable.
    """

    error_type = "no_active_device"
    status_code = 409


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


_logger = logging.getLogger(__name__)


def _request_id(request: Request) -> str:
    """Resolve the id for this request, wherever the handler is running."""
    stashed: str | None = getattr(request.state, "request_id", None)
    return stashed or current_request_id()


def _envelope(
    *,
    error_type: str,
    message: str,
    status_code: int,
    request_id: str,
    details: Any = None,  # noqa: ANN401
) -> JSONResponse:
    """Render the one error shape this service returns."""
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "type": error_type,
                "message": message,
                "details": details or {},
                "request_id": request_id,
            }
        },
    )


async def _handle_service_error(request: Request, exc: Exception) -> Response:
    """Render a deliberate failure using the status it declares."""
    assert isinstance(exc, ServiceError)  # noqa: S101 -- registered for this type only
    _logger.warning(
        "request failed", extra={"error_type": exc.error_type, "status": exc.status_code}
    )
    return _envelope(
        error_type=exc.error_type,
        message=exc.message,
        status_code=exc.status_code,
        request_id=_request_id(request),
        details=exc.details,
    )


async def _handle_validation_error(request: Request, exc: Exception) -> Response:
    """Render a schema violation, preserving pydantic's field-level detail."""
    assert isinstance(exc, RequestValidationError)  # noqa: S101 -- registered for this type
    return _envelope(
        error_type="validation_error",
        message="the request body is invalid",
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        request_id=_request_id(request),
        details={"errors": jsonable_encoder(exc.errors())},
    )


async def _handle_unexpected_error(request: Request, exc: Exception) -> Response:
    """Render anything we did not anticipate, without leaking its detail."""
    _logger.exception("unhandled exception", exc_info=exc)
    return _envelope(
        error_type="internal_error",
        message="an internal error occurred",
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        request_id=_request_id(request),
    )


def install_exception_handlers(app: FastAPI) -> None:
    """Register the handlers that render every failure as one envelope."""
    app.add_exception_handler(ServiceError, _handle_service_error)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
    app.add_exception_handler(Exception, _handle_unexpected_error)
