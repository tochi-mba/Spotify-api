"""The request contract is enforced at the edge, not in the handler."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from pydantic import ValidationError

from spotify_api.models.requests import MAX_TEXT_LENGTH, MIN_YEAR, LookupItem, LookupRequest


def test_only_name_is_required() -> None:
    item = LookupItem(name="Redbone")
    assert item.name == "Redbone"
    assert item.artist is None
    assert item.album is None
    assert item.year is None


def test_all_fields_round_trip() -> None:
    item = LookupItem(
        name="Bohemian Rhapsody", artist="Queen", album="A Night at the Opera", year=1975
    )
    assert item.model_dump() == {
        "name": "Bohemian Rhapsody",
        "artist": "Queen",
        "album": "A Night at the Opera",
        "year": 1975,
    }


@pytest.mark.parametrize("field", ["name", "artist", "album"])
def test_surrounding_whitespace_is_stripped(field: str) -> None:
    payload: dict[str, Any] = {"name": "x", field: "  padded  "}
    item = LookupItem(**payload)
    assert getattr(item, field) == "padded"


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_a_blank_name_is_rejected(blank: str) -> None:
    with pytest.raises(ValidationError):
        LookupItem(name=blank)


@pytest.mark.parametrize(("field", "blank"), [("artist", ""), ("album", "   ")])
def test_blank_optional_fields_become_null(field: str, blank: str) -> None:
    payload: dict[str, Any] = {"name": "x", field: blank}
    item = LookupItem(**payload)
    assert getattr(item, field) is None


@pytest.mark.parametrize("field", ["name", "artist", "album"])
def test_overlong_text_is_rejected(field: str) -> None:
    payload: dict[str, Any] = {"name": "x", field: "y" * (MAX_TEXT_LENGTH + 1)}
    with pytest.raises(ValidationError):
        LookupItem(**payload)


def test_year_lower_bound_is_enforced() -> None:
    assert LookupItem(name="x", year=MIN_YEAR).year == MIN_YEAR
    with pytest.raises(ValidationError):
        LookupItem(name="x", year=MIN_YEAR - 1)


def test_year_cannot_be_further_out_than_next_year() -> None:
    next_year = dt.datetime.now(tz=dt.UTC).year + 1
    assert LookupItem(name="x", year=next_year).year == next_year
    with pytest.raises(ValidationError):
        LookupItem(name="x", year=next_year + 1)


def test_unknown_fields_are_rejected_rather_than_silently_dropped() -> None:
    with pytest.raises(ValidationError) as excinfo:
        LookupItem(name="x", season=3)  # type: ignore[call-arg]
    assert "extra_forbidden" in str(excinfo.value)


def test_request_requires_at_least_one_item() -> None:
    with pytest.raises(ValidationError):
        LookupRequest(items=[])


def test_request_rejects_an_oversized_batch() -> None:
    items = [LookupItem(name=f"track {index}") for index in range(201)]
    with pytest.raises(ValidationError):
        LookupRequest(items=items)


def test_request_accepts_a_batch_and_preserves_order() -> None:
    request = LookupRequest(items=[LookupItem(name="a"), LookupItem(name="b")])
    assert [item.name for item in request.items] == ["a", "b"]
    assert request.market is None


@pytest.mark.parametrize(("given", "expected"), [("gb", "GB"), ("  us ", "US"), ("", None)])
def test_market_is_normalised(given: str, expected: str | None) -> None:
    assert LookupRequest(items=[LookupItem(name="a")], market=given).market == expected


@pytest.mark.parametrize("bad", ["GBR", "1", "g1"])
def test_invalid_market_codes_are_rejected(bad: str) -> None:
    with pytest.raises(ValidationError):
        LookupRequest(items=[LookupItem(name="a")], market=bad)


def test_request_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        LookupRequest(items=[LookupItem(name="a")], limit=5)  # type: ignore[call-arg]


@pytest.mark.parametrize("field", ["artist", "album", "year"])
def test_explicit_nulls_are_accepted(field: str) -> None:
    # A client that always emits every key sends `"artist": null` rather than
    # omitting it; that must behave exactly like omission.
    payload: dict[str, Any] = {"name": "x", field: None}
    assert getattr(LookupItem.model_validate(payload), field) is None


def test_explicit_null_market_is_accepted() -> None:
    request = LookupRequest.model_validate({"items": [{"name": "a"}], "market": None})
    assert request.market is None
