"""HTTP middleware.

A single middleware owns request identity and access logging, because the two
are inseparable: the id is what makes an access log line joinable to the
application log lines the request produced.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import TYPE_CHECKING

from starlette.middleware.base import BaseHTTPMiddleware

from spotify_api.logging import bind_request_id, reset_request_id

if TYPE_CHECKING:
    from starlette.middleware.base import RequestResponseEndpoint
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.types import ASGIApp

__all__ = ["MAX_REQUEST_ID_LENGTH", "REQUEST_ID_HEADER", "RequestContextMiddleware"]

#: Header used to accept an inbound correlation id and to echo the effective one.
REQUEST_ID_HEADER = "X-Request-ID"

#: Client-supplied ids longer than this are discarded, so a caller cannot use
#: the header to inflate every log line we write.
MAX_REQUEST_ID_LENGTH = 128

_access_logger = logging.getLogger("spotify_api.access")


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Bind a request id for the lifetime of the request and log the outcome."""

    def __init__(self, app: ASGIApp) -> None:
        """Wrap ``app``."""
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """Attach the request id, time the call, and emit one access log line."""
        request_id = self._resolve_request_id(request)
        # Exception handlers for unhandled errors run *outside* this middleware,
        # by which point the context var has been reset. Stashing the id on the
        # request keeps it reachable from there.
        request.state.request_id = request_id
        token = bind_request_id(request_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            self._log(request, status=500, started=started, level=logging.ERROR)
            raise
        else:
            response.headers[REQUEST_ID_HEADER] = request_id
            self._log(request, status=response.status_code, started=started, level=logging.INFO)
            return response
        finally:
            reset_request_id(token)

    @staticmethod
    def _resolve_request_id(request: Request) -> str:
        supplied = request.headers.get(REQUEST_ID_HEADER, "").strip()
        if supplied and len(supplied) <= MAX_REQUEST_ID_LENGTH:
            return supplied
        return str(uuid.uuid4())

    @staticmethod
    def _log(request: Request, *, status: int, started: float, level: int) -> None:
        _access_logger.log(
            level,
            "%s %s -> %s",
            request.method,
            request.url.path,
            status,
            extra={
                "http_method": request.method,
                "http_path": request.url.path,
                "http_status": status,
                "duration_ms": round((time.perf_counter() - started) * 1000, 3),
            },
            exc_info=level >= logging.ERROR,
        )
