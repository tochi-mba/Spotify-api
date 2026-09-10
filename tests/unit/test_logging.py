"""Structured logging emits parseable JSON and carries the request id."""

from __future__ import annotations

import contextvars
import json
import logging
import sys
from typing import TYPE_CHECKING

import pytest

from spotify_api.logging import (
    JsonFormatter,
    bind_request_id,
    configure_logging,
    current_request_id,
    install_request_id_record_factory,
    reset_request_id,
)

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def _clean_request_id() -> Iterator[None]:
    token = bind_request_id("fixture")
    yield
    reset_request_id(token)


def _record(**kwargs: object) -> logging.LogRecord:
    defaults: dict[str, object] = {
        "name": "spotify_api.test",
        "level": logging.INFO,
        "pathname": __file__,
        "lineno": 42,
        "msg": "hello",
        "args": (),
        "exc_info": None,
    }
    defaults.update(kwargs)
    return logging.LogRecord(**defaults)  # type: ignore[arg-type]


def test_formatter_emits_json_with_the_expected_envelope() -> None:
    payload = json.loads(JsonFormatter().format(_record()))
    assert payload["message"] == "hello"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "spotify_api.test"
    assert payload["service"] == "spotify-api"
    assert payload["request_id"] == "fixture"
    assert payload["timestamp"].endswith("+00:00")


def test_formatter_includes_structured_extras() -> None:
    record = _record()
    record.spotify_status = 429
    payload = json.loads(JsonFormatter().format(record))
    assert payload["spotify_status"] == 429


def test_formatter_renders_exceptions() -> None:
    try:
        message = "boom"
        raise ValueError(message)
    except ValueError:
        record = _record(exc_info=sys.exc_info())
    payload = json.loads(JsonFormatter().format(record))
    assert "ValueError: boom" in payload["exception"]


def test_request_id_defaults_to_a_dash_when_unbound() -> None:
    # A brand-new context has nothing bound, which is what a background task
    # or a non-HTTP entrypoint sees.
    assert contextvars.Context().run(current_request_id) == "-"


def test_bind_and_reset_are_symmetric() -> None:
    token = bind_request_id("inner")
    assert current_request_id() == "inner"
    reset_request_id(token)
    assert current_request_id() == "fixture"


@pytest.mark.parametrize("log_format", ["json", "console"])
def test_configure_logging_installs_exactly_one_handler(log_format: str) -> None:
    configure_logging(level="DEBUG", log_format=log_format)  # type: ignore[arg-type]
    configure_logging(level="DEBUG", log_format=log_format)  # type: ignore[arg-type]
    root = logging.getLogger()
    assert len(root.handlers) == 1
    assert root.level == logging.DEBUG
    is_json = isinstance(root.handlers[0].formatter, JsonFormatter)
    assert is_json is (log_format == "json")


def test_configure_logging_quietens_access_logs() -> None:
    configure_logging(level="INFO", log_format="json")
    assert logging.getLogger("uvicorn.access").propagate is False


def test_record_factory_stamps_the_id_at_creation_not_at_format_time() -> None:
    install_request_id_record_factory()
    token = bind_request_id("captured-early")
    record = logging.getLogger("spotify_api.test").makeRecord(
        "spotify_api.test", logging.INFO, __file__, 1, "later", (), None
    )
    reset_request_id(token)

    # The context has already moved on; the record must still know its origin.
    assert current_request_id() == "fixture"
    assert json.loads(JsonFormatter().format(record))["request_id"] == "captured-early"


def test_installing_the_record_factory_twice_does_not_stack_wrappers() -> None:
    install_request_id_record_factory()
    first = logging.getLogRecordFactory()
    install_request_id_record_factory()
    assert logging.getLogRecordFactory() is first


def test_formatter_falls_back_to_the_context_for_unstamped_records() -> None:
    # Records built by hand (or by a third-party library that replaced the
    # factory) still resolve an id rather than rendering nothing.
    payload = json.loads(JsonFormatter().format(_record()))
    assert payload["request_id"] == "fixture"
