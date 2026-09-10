"""Response models serialise to the documented envelope."""

from __future__ import annotations

import pytest

from spotify_api.models.requests import LookupItem
from spotify_api.models.responses import (
    Album,
    Artist,
    HealthResponse,
    LookupResponse,
    LookupResult,
    LookupStatus,
    ReadyResponse,
    Track,
)


@pytest.fixture
def track() -> Track:
    return Track(
        id="7tFiyTwD0nx5a1eklYtX2J",
        name="Bohemian Rhapsody",
        artists=[Artist(id="1dfeR4HaWDbWqFHLkxsg1d", name="Queen")],
        album=Album(
            id="1GbtB4zTqAsyfZEsm1RZfx",
            name="A Night at the Opera",
            release_date="1975-11-21",
            release_year=1975,
        ),
        duration_ms=354947,
        explicit=False,
        popularity=82,
        isrc="GBUM71029604",
        preview_url=None,
        external_url="https://open.spotify.com/track/7tFiyTwD0nx5a1eklYtX2J",
        uri="spotify:track:7tFiyTwD0nx5a1eklYtX2J",
    )


def test_lookup_status_values_are_the_documented_three() -> None:
    assert {status.value for status in LookupStatus} == {"found", "not_found", "error"}


def test_found_result_serialises_with_a_track_and_no_error(track: Track) -> None:
    result = LookupResult(
        index=0, query=LookupItem(name="Bohemian Rhapsody"), status=LookupStatus.FOUND, track=track
    )
    payload = result.model_dump(mode="json")
    assert payload["status"] == "found"
    assert payload["track"]["name"] == "Bohemian Rhapsody"
    assert payload["track"]["artists"][0]["name"] == "Queen"
    assert payload["error"] is None


def test_not_found_result_has_neither_track_nor_error() -> None:
    result = LookupResult(index=1, query=LookupItem(name="nope"), status=LookupStatus.NOT_FOUND)
    payload = result.model_dump(mode="json")
    assert payload["status"] == "not_found"
    assert payload["track"] is None
    assert payload["error"] is None


def test_error_result_carries_a_message() -> None:
    result = LookupResult(
        index=2,
        query=LookupItem(name="boom"),
        status=LookupStatus.ERROR,
        error="upstream refused the request",
    )
    assert result.model_dump(mode="json")["error"] == "upstream refused the request"


def test_response_count_is_derived_from_the_results(track: Track) -> None:
    results = [
        LookupResult(index=0, query=LookupItem(name="a"), status=LookupStatus.FOUND, track=track),
        LookupResult(index=1, query=LookupItem(name="b"), status=LookupStatus.NOT_FOUND),
    ]
    response = LookupResponse(request_id="req-1", results=results)
    assert response.count == 2
    assert response.model_dump(mode="json")["count"] == 2


def test_response_count_cannot_be_faked() -> None:
    payload = {
        "request_id": "req-1",
        "count": 99,
        "results": [
            {
                "index": 0,
                "query": {"name": "a"},
                "status": "not_found",
                "track": None,
                "error": None,
            }
        ],
    }
    assert LookupResponse.model_validate(payload).count == 1


def test_health_response_shape() -> None:
    payload = HealthResponse(version="0.1.0", uptime_seconds=1.5).model_dump(mode="json")
    assert payload == {
        "status": "ok",
        "service": "spotify-api",
        "version": "0.1.0",
        "uptime_seconds": 1.5,
    }


@pytest.mark.parametrize(
    ("ready", "status", "dependency"),
    [(True, "ready", "ok"), (False, "not_ready", "unavailable")],
)
def test_ready_response_reflects_dependency_health(
    ready: bool, status: str, dependency: str
) -> None:
    payload = ReadyResponse.from_spotify_health(spotify_ok=ready).model_dump(mode="json")
    assert payload == {"status": status, "dependencies": {"spotify": dependency}}


def test_album_release_year_may_be_absent_for_odd_release_dates() -> None:
    album = Album(id="x", name="y", release_date="unknown", release_year=None)
    assert album.model_dump(mode="json")["release_year"] is None
