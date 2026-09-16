"""Translating exceptions into RFC 9457 problem responses.

This is the only place in the service that maps a failure to a status code. Everything below
the api layer raises a :class:`~spotify_api.errors.ServiceError` and lets this decide what that
means over HTTP -- which is what keeps the Spotify adapter, the credential provider and the job
runner free of the web framework, and the problem document identical wherever a failure began.

Three of the mappings are decisions rather than lookups.

**A refused token is 401; keyring being unreachable is 503.** A 401 tells the caller to fetch a
new token, and they would be fetching one because *we* could not reach keyring's public keys.

**No active device is 409, not 404.** Spotify reports it as a bare 404, which reads as "that
does not exist". The player exists and is in the wrong state, and the fix -- open Spotify on a
device -- is the caller's to make.

**An unexpected exception's text never reaches the caller.** It can carry a URL, and a URL can
carry credentials. The caller gets a request id to quote, and the log has the stack.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from spotify_api.context import get_request_id
from spotify_api.errors import (
    BatchTooLargeError,
    ConfirmationTimeoutError,
    CredentialUnavailableError,
    JobNotFoundError,
    KeyringUnavailableError,
    NoActiveDeviceError,
    PreferencesUnavailableError,
    PremiumRequiredError,
    ServiceError,
    SpotifyAuthError,
    SpotifyRateLimitError,
    SpotifyUnavailableError,
    TrackLookupError,
    UserTokenRejectedError,
)
from spotify_api.logging import get_logger
from spotify_api.models.responses import PROBLEM_CONTENT_TYPE, FieldError, Problem

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Mapping
    from typing import Any

__all__ = [
    "PROBLEM_BASE_URI",
    "WWW_AUTHENTICATE",
    "problem_response",
    "register_exception_handlers",
    "unhandled_problem_response",
]

_logger = get_logger(__name__)

PROBLEM_BASE_URI = "https://spotify-api.invalid/problems"

WWW_AUTHENTICATE = "Bearer"
"""The challenge every 401 carries, as RFC 9110 requires one to.

Attached here rather than at each call site so that no future 401 can forget it."""

_STATUS_TITLES = {
    status.HTTP_401_UNAUTHORIZED: "Unauthenticated",
    status.HTTP_403_FORBIDDEN: "Forbidden",
    status.HTTP_404_NOT_FOUND: "Not found",
    status.HTTP_405_METHOD_NOT_ALLOWED: "Method not allowed",
    status.HTTP_409_CONFLICT: "Conflict",
    status.HTTP_422_UNPROCESSABLE_CONTENT: "Validation failed",
    status.HTTP_500_INTERNAL_SERVER_ERROR: "Internal server error",
    status.HTTP_502_BAD_GATEWAY: "Bad gateway",
    status.HTTP_503_SERVICE_UNAVAILABLE: "Service unavailable",
    status.HTTP_504_GATEWAY_TIMEOUT: "Gateway timeout",
}

# Domain errors that map cleanly onto a status code. Anything absent is a bug and becomes a 500
# with its detail withheld -- and a test walks the hierarchy, so it cannot happen by omission.
_DOMAIN_STATUS: dict[type[ServiceError], int] = {
    UserTokenRejectedError: status.HTTP_401_UNAUTHORIZED,
    # Named apart from every other refusal: "upgrade the account" is advice a caller can act on.
    PremiumRequiredError: status.HTTP_403_FORBIDDEN,
    # Unknown, expired and another account's are one answer, so the status confirms nothing.
    JobNotFoundError: status.HTTP_404_NOT_FOUND,
    NoActiveDeviceError: status.HTTP_409_CONFLICT,
    # The operational cap: a body the schema accepts and this deployment will not run.
    BatchTooLargeError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    # Keyring answered and holds nothing usable for this person, so retrying cannot help.
    CredentialUnavailableError: status.HTTP_502_BAD_GATEWAY,
    TrackLookupError: status.HTTP_502_BAD_GATEWAY,
    KeyringUnavailableError: status.HTTP_503_SERVICE_UNAVAILABLE,
    PreferencesUnavailableError: status.HTTP_503_SERVICE_UNAVAILABLE,
    SpotifyAuthError: status.HTTP_503_SERVICE_UNAVAILABLE,
    SpotifyRateLimitError: status.HTTP_503_SERVICE_UNAVAILABLE,
    SpotifyUnavailableError: status.HTTP_503_SERVICE_UNAVAILABLE,
    # Spotify accepted the command; its effect never became observable in time.
    ConfirmationTimeoutError: status.HTTP_504_GATEWAY_TIMEOUT,
}


# PLR0913: seven keyword-only fields, most of them RFC 9457's own. Grouping them into an object
# would add a type whose only job is to be unpacked one line later.
def problem_response(  # noqa: PLR0913
    *,
    status_code: int,
    detail: str,
    instance: str,
    problem_type: str | None = None,
    errors: list[FieldError] | None = None,
    details: Mapping[str, object] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """Build a problem+json response carrying the current request id.

    Args:
        status_code: The HTTP status.
        detail: What went wrong this time, written for the caller. Never an exception's text.
        instance: The path of the request that failed.
        problem_type: The last segment of ``type``. Defaults to one derived from the status.
        errors: Field-level validation failures, when that is what this is.
        details: Structured context for this occurrence, when there is any.
        headers: Headers the failure itself obliges the response to carry, such as a 405's
            ``Allow``.
    """
    problem = Problem(
        type=f"{PROBLEM_BASE_URI}/{problem_type or _slug_for(status_code)}",
        title=_STATUS_TITLES.get(status_code, "Error"),
        status=status_code,
        detail=detail,
        instance=instance,
        request_id=get_request_id(),
        errors=errors,
        details=dict(details) if details else None,
    )
    # Absent rather than null, and at the top level only: a null inside `details` -- a player
    # state with no context loaded, say -- is information, and is kept.
    content = {
        key: value for key, value in problem.model_dump(mode="json").items() if value is not None
    }
    return JSONResponse(
        status_code=status_code,
        content=content,
        media_type=PROBLEM_CONTENT_TYPE,
        headers={**(headers or {}), **_headers_for(status_code)},
    )


def _headers_for(status_code: int) -> dict[str, str]:
    """Headers a status code obliges the response to carry, wherever it is produced."""
    if status_code == status.HTTP_401_UNAUTHORIZED:
        return {"WWW-Authenticate": WWW_AUTHENTICATE}
    return {}


def _slug_for(status_code: int) -> str:
    return _STATUS_TITLES.get(status_code, "error").lower().replace(" ", "-")


def register_exception_handlers(app: FastAPI) -> None:
    """Install every handler the app needs. Called once, by the app factory."""

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        """Reshape FastAPI's validation errors into the one error format this API uses.

        Only the location and the message are copied. FastAPI's raw errors include the offending
        **input** -- a track name, a device id, a whole body that was not an object -- and the
        default handler would echo it into the response.
        """
        return problem_response(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="the request failed validation",
            instance=request.url.path,
            problem_type="validation-failed",
            errors=[
                FieldError(
                    location=".".join(str(part) for part in error["loc"]),
                    message=error["msg"],
                )
                for error in exc.errors()
            ],
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        """An unknown path or a method the route does not take, in the same shape as the rest.

        The exception's headers are kept: a 405 must say which methods are allowed.
        """
        return problem_response(
            status_code=exc.status_code,
            detail=str(exc.detail),
            instance=request.url.path,
            headers=exc.headers,
        )

    for error_type, status_code in _DOMAIN_STATUS.items():
        app.add_exception_handler(error_type, _domain_handler(status_code))


def unhandled_problem_response(exc: BaseException, *, instance: str) -> JSONResponse:
    """Render an unexpected exception as a 500.

    The exception's own message is withheld: it can carry a URL, and a URL can carry
    credentials. The request id ties the response to the log record that has the stack --
    which is why this is invoked from inside
    :class:`~spotify_api.api.middleware.RequestContextMiddleware`, while the id is still bound,
    rather than from Starlette's outermost error middleware, where the binding has already
    unwound and the response would carry no id at all.

    Only the exception's **type name** and stack are logged, never its message; see
    :func:`spotify_api.logging.render_exception`.
    """
    _logger.exception("unhandled_exception", error_type=type(exc).__name__, exc_info=exc)
    return problem_response(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="an unexpected error occurred; quote the request id when reporting it",
        instance=instance,
    )


def _domain_handler(
    status_code: int,
) -> Callable[[Request, Exception], Coroutine[Any, Any, JSONResponse]]:
    """Build a handler that renders a domain error at ``status_code``."""

    async def handler(request: Request, exc: Exception) -> JSONResponse:
        # Registered for ServiceError subclasses only, so this names the type rather than
        # checking it.
        error = cast("ServiceError", exc)
        _logger.warning("service_error", error_type=error.error_type, status_code=status_code)
        return problem_response(
            status_code=status_code,
            detail=error.message,
            instance=request.url.path,
            problem_type=error.error_type.replace("_", "-"),
            details=error.details,
        )

    return handler
