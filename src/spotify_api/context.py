"""Per-request context.

Two facts are attached once, at the edge, and read wherever they are needed -- log records,
problem responses -- without being threaded through every signature: the request id, and the
account the caller's verified token names. Context variables are task-local, so two people's
concurrent requests never see each other's, and a background job inherits the values of the
request that started it, because a task copies the context it was created in.

The account is here for observability, not for authorization. Everything that acts on an
account -- the job store above all -- takes it as an argument: a store method that reached into
a context variable for it would behave differently depending on who called it, and could not
be reasoned about from its signature.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

__all__ = [
    "bind_request_id",
    "get_account_id",
    "get_request_id",
    "new_request_id",
    "set_account_id",
]

_request_id: ContextVar[str | None] = ContextVar("spotify_api_request_id", default=None)
_account_id: ContextVar[str | None] = ContextVar("spotify_api_account_id", default=None)


def new_request_id() -> str:
    """Return a fresh request id."""
    return str(uuid.uuid4())


def get_request_id() -> str | None:
    """Return the current request id, or ``None`` outside a request."""
    return _request_id.get()


@contextmanager
def bind_request_id(request_id: str) -> Iterator[str]:
    """Bind ``request_id`` for the duration of the block, restoring the previous value after."""
    token = _request_id.set(request_id)
    try:
        yield request_id
    finally:
        _request_id.reset(token)


def get_account_id() -> str | None:
    """Return the verified account this request acts for, or ``None`` before one is known."""
    return _account_id.get()


def set_account_id(account_id: str) -> None:
    """Bind ``account_id`` for the remainder of the current task.

    There is no matching unbind, because the caller is a FastAPI dependency: the binding has to
    outlive the dependency and cover the handler, and a context manager cannot span the two.
    The binding ends with the request's task rather than leaking into the next request the same
    worker serves, which is the property that makes this safe.
    """
    _account_id.set(account_id)
