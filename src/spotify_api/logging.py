"""Structured logging.

Every log line is a single JSON object carrying the id of the request that
produced it, which makes a batch lookup traceable end to end across the route,
the resolver and the Spotify client.

The request id lives in a :class:`~contextvars.ContextVar` so it propagates
through ``asyncio.gather`` without being threaded manually through every call.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from contextvars import ContextVar, Token
from typing import Any, Literal

from spotify_api import SERVICE_NAME

__all__ = [
    "JsonFormatter",
    "bind_request_id",
    "configure_logging",
    "current_request_id",
    "install_request_id_record_factory",
    "reset_request_id",
]

_REQUEST_ID: ContextVar[str] = ContextVar("request_id", default="-")

#: Attributes present on every ``LogRecord``; anything else was supplied by the
#: caller via ``extra=`` and is promoted to a top-level field.
_RESERVED = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
        "request_id",
    }
)


def bind_request_id(request_id: str) -> Token[str]:
    """Bind ``request_id`` to the current context and return an undo token."""
    return _REQUEST_ID.set(request_id)


def reset_request_id(token: Token[str]) -> None:
    """Undo a previous :func:`bind_request_id`."""
    _REQUEST_ID.reset(token)


def current_request_id() -> str:
    """Return the request id bound to the current context, or ``"-"``."""
    return _REQUEST_ID.get()


def install_request_id_record_factory() -> None:
    """Stamp the active request id onto every ``LogRecord`` as it is created.

    Formatting can happen long after the fact -- a queued handler, a test that
    captures records and renders them later -- by which point the context has
    moved on. Capturing the id at creation makes it a property of the record
    rather than of whoever formats it. Idempotent.
    """
    existing = logging.getLogRecordFactory()
    if getattr(existing, "_stamps_request_id", False):
        return

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:  # noqa: ANN401
        record = existing(*args, **kwargs)
        record.request_id = current_request_id()
        return record

    factory._stamps_request_id = True  # type: ignore[attr-defined]  # noqa: SLF001
    logging.setLogRecordFactory(factory)


install_request_id_record_factory()


class JsonFormatter(logging.Formatter):
    """Render a :class:`logging.LogRecord` as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        """Serialise ``record`` to JSON."""
        payload: dict[str, Any] = {
            "timestamp": dt.datetime.fromtimestamp(record.created, tz=dt.UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "service": SERVICE_NAME,
            # Stamped at record-creation time by the record factory, so the id
            # survives handlers that format lazily or on another thread.
            "request_id": getattr(record, "request_id", None) or current_request_id(),
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(
    *, level: str = "INFO", log_format: Literal["json", "console"] = "json"
) -> None:
    """Install a single root handler using the requested rendering.

    Idempotent: calling it again replaces the existing handler rather than
    stacking a second one, so repeated calls (tests, reloads) cannot duplicate
    output.
    """
    formatter: logging.Formatter = (
        JsonFormatter()
        if log_format == "json"
        else logging.Formatter(
            "%(asctime)s %(levelname)-8s %(name)s :: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    install_request_id_record_factory()

    handler = logging.StreamHandler()
    handler.setFormatter(formatter)

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)

    # uvicorn ships its own access log; ours already records method, path,
    # status and duration with the request id attached.
    logging.getLogger("uvicorn.access").propagate = False
