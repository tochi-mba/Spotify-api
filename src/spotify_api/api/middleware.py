"""Request-scoped cross-cutting behaviour.

One middleware, doing what belongs to a single span: give the request an id, make that id
visible to every log record the request produces, record how long it took, and turn anything
nobody anticipated into a problem document that still carries the id. The verified account is
bound separately, by the dependency that checks the token, because it is not known until the
route's dependencies run.

:func:`time.perf_counter` here is the one deliberate exception to "inject time". It measures a
duration for a log field and a header and decides nothing, so injecting a clock would buy a
test the ability to assert a number nobody branches on.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from starlette.middleware.base import BaseHTTPMiddleware

from spotify_api.api.errors import unhandled_problem_response
from spotify_api.context import bind_request_id, new_request_id
from spotify_api.logging import get_logger

if TYPE_CHECKING:
    from starlette.middleware.base import RequestResponseEndpoint
    from starlette.requests import Request
    from starlette.responses import Response

__all__ = [
    "MAX_REQUEST_ID_LENGTH",
    "REQUEST_ID_HEADER",
    "RESPONSE_TIME_HEADER",
    "RequestContextMiddleware",
]

_logger = get_logger(__name__)

#: Header used to accept an inbound correlation id and to echo the effective one.
REQUEST_ID_HEADER = "X-Request-ID"

#: How long the request took, in milliseconds, as every service in the family reports it.
RESPONSE_TIME_HEADER = "X-Response-Time-Ms"

#: Client-supplied ids longer than this are discarded, so a caller cannot use the header to
#: inflate every log line we write.
MAX_REQUEST_ID_LENGTH = 128


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Binds a request id and logs the outcome of every request."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """Attach the request id, time the call, and emit one completion record."""
        request_id = _resolve_request_id(request)
        started = time.perf_counter()
        with bind_request_id(request_id):
            try:
                response = await call_next(request)
            except Exception as exc:
                # Handled here rather than by Starlette's outermost error middleware, which runs
                # after this binding has unwound and would answer with no request id -- the one
                # thing the caller is told to quote.
                _logger.warning(
                    "request_failed",
                    method=request.method,
                    path=request.url.path,
                    duration_ms=_elapsed_ms(started),
                )
                response = unhandled_problem_response(exc, instance=request.url.path)

            duration_ms = _elapsed_ms(started)
            _logger.info(
                "request_completed",
                method=request.method,
                path=request.url.path,
                status_code=response.status_code,
                duration_ms=duration_ms,
            )

        response.headers[REQUEST_ID_HEADER] = request_id
        response.headers[RESPONSE_TIME_HEADER] = f"{duration_ms:.1f}"
        return response


def _resolve_request_id(request: Request) -> str:
    """The caller's id when it is usable, so a trace can span services; a fresh one otherwise.

    An over-long id is replaced rather than truncated: a truncated id would look like the
    caller's own and match none of their records.
    """
    supplied = request.headers.get(REQUEST_ID_HEADER, "").strip()
    if supplied and len(supplied) <= MAX_REQUEST_ID_LENGTH:
        return supplied
    return new_request_id()


def _elapsed_ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000
