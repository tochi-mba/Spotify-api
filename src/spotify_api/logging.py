"""Structured logging, with a redaction pass that runs before anything is rendered.

JSON in deployment so records are queryable, console output locally, chosen by
``SPOTIFY_API_LOG_FORMAT``. Every record carries the request id, and the verified account once
the caller's token has been checked -- which is what makes a batch lookup or a playback command
traceable across the route, the resolver, the Spotify client and the job that ran it.

## What must never be recorded

This service handles three bearer credentials, each worth stealing: the caller's keyring user
token, this service's own keyring service token, and the Spotify access token keyring hands
over inside a header. None of them may reach a log record. Nor may an exception's message: the
text an HTTP client raises carries the URL it was given, and a URL can carry credentials.

There are two mechanisms and both are needed:

* **No call site passes a credential, or an exception's text, to a logger at all.** A failure
  is logged by its exception *type* name. That is a rule about call sites rather than about
  this module.
* **This module catches the call site that forgot.** The redactor replaces every
  secret-looking field, by name, at every depth, before anything is rendered; and a traceback is
  rendered as its stack and its type, never its message.

Belt and braces. A test drives the whole stack with real tokens -- through a success, a refused
token, keyring failing and an unhandled exception -- and asserts that neither the user token nor
the service token appears in any record.
"""

from __future__ import annotations

import logging
import sys
import traceback
from typing import TYPE_CHECKING, Any

import structlog

from spotify_api.context import get_account_id, get_request_id

if TYPE_CHECKING:
    from structlog.typing import EventDict, Processor, WrappedLogger

    from spotify_api.config import LogFormat

__all__ = [
    "MAX_REDACTION_DEPTH",
    "REDACTED",
    "add_account_id",
    "add_request_id",
    "configure_logging",
    "get_logger",
    "is_sensitive",
    "redact_secrets",
    "render_exception",
]

REDACTED = "[redacted]"
"""What a sensitive value is replaced with. A constant so tests can assert on it."""

MAX_REDACTION_DEPTH = 6
"""How far into nested structures the redactor walks before giving up and dropping."""

_SENSITIVE_SUBSTRINGS = (
    "authorization",
    "cookie",
    "credential",
    "passphrase",
    "password",
    "passwd",
    "private_key",
    "secret",
    "token",
    "api_key",
)
"""Substrings that make a field name sensitive wherever it appears.

Matched as substrings rather than exact names so ``user_token``, ``service_token`` and
``refresh_token`` are covered without maintaining a list of every compound. The failure mode of
an over-broad rule is a redacted field that did not need it; the failure mode of a narrow one is
a credential in a log file.
"""


def is_sensitive(field: str) -> bool:
    """Whether a field name means the value must not be recorded."""
    lowered = field.lower()
    return any(marker in lowered for marker in _SENSITIVE_SUBSTRINGS)


def redact_secrets(
    _logger: WrappedLogger | None,
    _method_name: str,
    event_dict: EventDict,
) -> EventDict:
    """Replace every sensitive value in the record, however deeply it is nested."""
    return {key: _redact_value(key, value, depth=0) for key, value in event_dict.items()}


def _redact_value(key: str, value: object, *, depth: int) -> Any:  # noqa: ANN401
    """Redact one key/value pair, recursing into containers."""
    if is_sensitive(key):
        # Replaced wholesale rather than masked: the length and type of a value are
        # themselves information, and `token=None` would reveal that none was sent.
        return REDACTED
    return _redact_container(value, depth=depth)


def _redact_container(value: object, *, depth: int) -> Any:  # noqa: ANN401
    """Walk into a dict or list, or return the value untouched."""
    if not isinstance(value, dict | list):
        return value

    if depth >= MAX_REDACTION_DEPTH:
        # Fail closed. A structure this deep is either a bug or an attempt to bury
        # something past the walker, and neither deserves to be rendered.
        return REDACTED

    if isinstance(value, dict):
        # A key that is not a string is redacted wholesale rather than rendered and then
        # name-checked: a bytes or tuple key stringifies to something no name rule was written
        # for. Nothing in this service logs a non-string key, so failing closed costs nothing
        # and removes a way in.
        return {
            str(key): (
                _redact_value(key, item, depth=depth + 1) if isinstance(key, str) else REDACTED
            )
            for key, item in value.items()
        }
    return [_redact_container(item, depth=depth + 1) for item in value]


def render_exception(
    _logger: WrappedLogger | None,
    _method_name: str,
    event_dict: EventDict,
) -> EventDict:
    """Render an attached exception as its stack and its type name -- never its message.

    structlog's own ``format_exc_info`` renders a traceback exactly as Python prints one, and
    Python ends it with the exception's message: ``httpx.ConnectError`` carries the URL it
    failed to reach, and a ``ValueError`` raised while parsing a token can carry the token. The
    stack -- files, lines, functions -- is what makes an unexpected failure debuggable, and none
    of it is data from the request. So the stack is kept and the message is dropped.

    ``exc_info`` is always popped, whatever it held: a renderer that found it still there would
    format the exception itself, message and all.
    """
    error = _exception_of(event_dict.pop("exc_info", None))
    if error is not None:
        frames = traceback.format_list(traceback.extract_tb(error.__traceback__))
        event_dict["exception"] = "".join(
            ["Traceback (most recent call last):\n", *frames, type(error).__name__]
        )
    return event_dict


def _exception_of(exc_info: object) -> BaseException | None:
    """The exception ``exc_info`` names, in any of the spellings structlog accepts."""
    if exc_info is True:
        # `logger.exception(...)` inside an `except` block: the one being handled.
        exc_info = sys.exc_info()
    if isinstance(exc_info, tuple):
        exc_info = exc_info[1]
    return exc_info if isinstance(exc_info, BaseException) else None


def add_request_id(
    _logger: WrappedLogger | None,
    _method_name: str,
    event_dict: EventDict,
) -> EventDict:
    """Attach the bound request id, if there is one.

    Absent outside a request rather than present-and-null, so queries can filter on
    existence.
    """
    request_id = get_request_id()
    if request_id is not None:
        event_dict["request_id"] = request_id
    return event_dict


def add_account_id(
    _logger: WrappedLogger | None,
    _method_name: str,
    event_dict: EventDict,
) -> EventDict:
    """Attach the verified account, once the caller's token has been checked.

    The account id is keyring's opaque identifier, not an email address -- this service never
    learns one. It is safe to record, and it is what makes "why did this person's playback
    command fail" answerable without correlating by hand.
    """
    account_id = get_account_id()
    if account_id is not None:
        event_dict["account_id"] = account_id
    return event_dict


def configure_logging(*, level: str, log_format: LogFormat) -> None:
    """Configure structlog process-wide. Safe to call again to change the configuration."""
    shared: list[Processor] = [
        structlog.processors.add_log_level,
        add_request_id,
        add_account_id,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
        render_exception,
        # Last before rendering, so it also covers anything the processors above added.
        redact_secrets,
    ]
    renderer: Processor = (
        structlog.processors.JSONRenderer()
        if log_format == "json"
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    structlog.configure(
        processors=[*shared, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[level.upper()]
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )


class _NamedLogger:
    """A logger that resolves against the configuration in force *when it is called*.

    This exists because of a defect found in a sibling service, and the defect is worth
    writing down because the obvious spelling has it.

    ``structlog.get_logger().bind(logger=name)`` binds **eagerly**: the proxy ``get_logger()``
    returns is lazy, but ``.bind()`` resolves it there and then against whatever configuration
    is in force, and the resulting logger keeps that processor chain for ever. Every module here
    does ``_logger = get_logger(__name__)`` at import time, which is necessarily *before*
    :func:`configure_logging` has run -- so every module-level logger would be permanently wired
    to structlog's defaults, ``SPOTIFY_API_LOG_FORMAT=json`` would validate and do nothing, and
    the redactor would never run.

    Resolving per call costs a dictionary lookup and a bind on each record, at a volume of a few
    records per request.
    """

    __slots__ = ("_name",)

    def __init__(self, name: str) -> None:
        self._name = name

    def __getattr__(self, method: str) -> Any:  # noqa: ANN401
        # The bind happens here, on the way to `.info` or `.exception`, so it picks up
        # whatever configure_logging last installed.
        return getattr(structlog.get_logger().bind(logger=self._name), method)


def get_logger(name: str) -> Any:  # noqa: ANN401
    """Return a logger tagged with ``name``.

    The name is bound into the event dict rather than read off the underlying logger, so it
    survives regardless of which logger factory is configured.

    The return type is deliberately loose: structlog's filtering bound loggers are generated at
    configuration time and have no single static type. It is also the shape
    :mod:`keyring_client` accepts as ``logger=``, which is why the app hands one to the key
    cache and the token verifier.
    """
    return _NamedLogger(name)
