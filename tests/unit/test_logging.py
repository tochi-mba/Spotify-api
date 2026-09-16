"""Structured logging, and the redaction pass that runs before anything is rendered.

Two mechanisms keep a credential out of a log record: no call site passes one, and this module
catches the call site that forgot. This file pins the second -- the redactor, and the exception
renderer that keeps a traceback's stack while dropping its message. It also pins the
``_NamedLogger`` defect: a module-level logger created before ``configure_logging`` ran must
still honour the configuration installed afterwards, or ``SPOTIFY_API_LOG_FORMAT=json`` does
nothing and the redactor never runs.
"""

from __future__ import annotations

import contextvars
import json
from typing import TYPE_CHECKING, Any

import pytest

from spotify_api import logging as logging_module
from spotify_api.context import bind_request_id, set_account_id
from spotify_api.logging import (
    MAX_REDACTION_DEPTH,
    REDACTED,
    add_account_id,
    add_request_id,
    configure_logging,
    get_logger,
    is_sensitive,
    redact_secrets,
    render_exception,
)

if TYPE_CHECKING:
    from spotify_api.config import LogFormat

SENTINEL = "sentinel-credential-7f3a"

module_logger = get_logger("tests.module_level")
"""Created at import, before any configure_logging call -- exactly like every module in src."""


def last_record(out: str) -> dict[str, Any]:
    record: dict[str, Any] = json.loads(out.strip().splitlines()[-1])
    return record


def explode() -> None:
    message = f"failed https://user:{SENTINEL}@keyring.test/v1/internal"
    raise ValueError(message)


# -- which names are sensitive --------------------------------------------------


@pytest.mark.parametrize(
    "field",
    [
        "authorization",
        "Authorization",
        "user_token",
        "service_token",
        "keyring_service_token",
        "refresh_token",
        "client_secret",
        "api_key",
        "x_api_key",
        "password",
        "passwd",
        "passphrase",
        "cookie",
        "private_key",
        "credential",
    ],
)
def test_secret_looking_names_are_sensitive_wherever_they_appear(field: str) -> None:
    assert is_sensitive(field) is True


@pytest.mark.parametrize(
    "field",
    [
        "event",
        "logger",
        "request_id",
        "account_id",
        "job_id",
        "error_type",
        "status_code",
        "retry_after",
        "item_index",
        "method",
        "path",
        "replacement",
    ],
)
def test_the_fields_this_service_logs_are_not_sensitive(field: str) -> None:
    assert is_sensitive(field) is False


# -- redaction ------------------------------------------------------------------


def test_a_sensitive_top_level_field_is_replaced_wholesale() -> None:
    record = redact_secrets(None, "info", {"event": "x", "user_token": SENTINEL, "job_id": "j"})
    # Wholesale rather than masked: the length and type of a value are themselves information.
    assert record == {"event": "x", "user_token": REDACTED, "job_id": "j"}


def test_a_credential_nested_in_headers_is_replaced() -> None:
    record = redact_secrets(
        None,
        "info",
        {"headers": {"Authorization": f"Bearer {SENTINEL}", "Accept": "application/json"}},
    )
    assert record == {"headers": {"Authorization": REDACTED, "Accept": "application/json"}}


def test_lists_are_walked() -> None:
    record = redact_secrets(None, "info", {"items": [{"password": "p", "n": 1}, "plain"]})
    assert record == {"items": [{"password": REDACTED, "n": 1}, "plain"]}


def test_a_structure_past_the_depth_limit_is_dropped_whole() -> None:
    deep: dict[str, Any] = {"n": 0}
    node = deep
    for level in range(MAX_REDACTION_DEPTH + 2):
        node["child"] = {"n": level + 1}
        node = node["child"]
    record = redact_secrets(None, "info", {"deep": deep})
    # Fail closed: a structure this deep is a bug or an attempt to bury something past the
    # walker, and neither deserves to be rendered.
    rendered = json.dumps(record)
    assert REDACTED in rendered
    assert rendered.count("child") < MAX_REDACTION_DEPTH + 2


def test_a_non_string_dict_key_is_redacted_wholesale() -> None:
    record = redact_secrets(None, "info", {"outer": {b"raw": "bytes", 7: "seven", "ok": 1}})
    assert record == {"outer": {"b'raw'": REDACTED, "7": REDACTED, "ok": 1}}


def test_scalars_pass_through_untouched() -> None:
    record = {"n": 1, "f": 1.5, "s": "x", "b": True, "none": None}
    assert redact_secrets(None, "info", dict(record)) == record


# -- exceptions -----------------------------------------------------------------


def test_the_exception_being_handled_is_rendered_as_its_stack_and_type_only() -> None:
    try:
        explode()
    except ValueError:
        record = render_exception(None, "error", {"event": "x", "exc_info": True})

    assert "exc_info" not in record
    # The stack is kept: it is what makes an unexpected failure debuggable.
    assert "explode" in record["exception"]
    assert record["exception"].endswith("ValueError")
    assert SENTINEL not in record["exception"]


def test_an_exception_instance_is_rendered_the_same_way() -> None:
    with pytest.raises(ValueError, match="keyring") as caught:
        explode()

    record = render_exception(None, "error", {"event": "x", "exc_info": caught.value})

    assert "explode" in record["exception"]
    assert SENTINEL not in record["exception"]


def test_an_exc_info_tuple_is_rendered_the_same_way() -> None:
    with pytest.raises(ValueError, match="keyring") as caught:
        explode()

    exc_info = (caught.type, caught.value, caught.tb)
    record = render_exception(None, "error", {"event": "x", "exc_info": exc_info})

    assert "explode" in record["exception"]
    assert SENTINEL not in record["exception"]


def test_asking_for_the_exception_when_none_is_being_handled_renders_nothing() -> None:
    assert render_exception(None, "error", {"event": "x", "exc_info": True}) == {"event": "x"}


def test_a_record_without_an_exception_is_left_alone() -> None:
    assert render_exception(None, "info", {"event": "x"}) == {"event": "x"}


# -- context --------------------------------------------------------------------


def test_context_fields_are_absent_rather_than_null_outside_a_request() -> None:
    def process() -> dict[str, Any]:
        record: dict[str, Any] = {"event": "x"}
        add_request_id(None, "info", record)
        add_account_id(None, "info", record)
        return record

    assert contextvars.Context().run(process) == {"event": "x"}


def test_context_fields_are_present_when_bound() -> None:
    def process() -> dict[str, Any]:
        record: dict[str, Any] = {"event": "x"}
        set_account_id("acct-1")
        with bind_request_id("req-1"):
            add_request_id(None, "info", record)
            add_account_id(None, "info", record)
        return record

    assert contextvars.Context().run(process) == {
        "event": "x",
        "request_id": "req-1",
        "account_id": "acct-1",
    }


# -- the configured pipeline ----------------------------------------------------


def test_a_logger_made_before_configuration_honours_json_configured_after(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(level="INFO", log_format="json")
    module_logger.info("probe", job_id="j-1", user_token=SENTINEL)

    record = last_record(capsys.readouterr().out)
    assert record["event"] == "probe"
    assert record["logger"] == "tests.module_level"
    assert record["level"] == "info"
    assert record["job_id"] == "j-1"
    assert record["user_token"] == REDACTED
    assert "timestamp" in record


def test_switching_to_console_takes_effect_on_the_same_logger(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(level="INFO", log_format="console")
    module_logger.info("probe", service_token=SENTINEL)

    line = capsys.readouterr().out.strip().splitlines()[-1]
    with pytest.raises(json.JSONDecodeError):
        json.loads(line)
    assert "probe" in line
    assert SENTINEL not in line


def test_the_level_filters(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="WARNING", log_format="json")
    module_logger.info("quiet")
    module_logger.warning("loud")

    out = capsys.readouterr().out
    assert "quiet" not in out
    assert "loud" in out


def test_the_request_context_reaches_the_record(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="INFO", log_format="json")

    def log() -> None:
        set_account_id("acct-9")
        with bind_request_id("req-9"):
            module_logger.info("probe")

    contextvars.Context().run(log)
    record = last_record(capsys.readouterr().out)
    assert (record["request_id"], record["account_id"]) == ("req-9", "acct-9")


@pytest.mark.parametrize("log_format", ["json", "console"])
def test_a_logged_exception_never_renders_its_message_in_either_format(
    capsys: pytest.CaptureFixture[str], log_format: LogFormat
) -> None:
    # Handed a raw exception, the console renderer would format it itself -- message and all,
    # with whichever traceback library happens to be installed.
    configure_logging(level="INFO", log_format=log_format)
    try:
        explode()
    except ValueError:
        module_logger.exception("failed", error_type="ValueError")

    out = capsys.readouterr().out
    assert "explode" in out
    assert SENTINEL not in out


def test_get_logger_returns_the_late_binding_proxy() -> None:
    assert isinstance(get_logger("x"), logging_module._NamedLogger)
