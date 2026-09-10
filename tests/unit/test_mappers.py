"""Raw Spotify JSON is translated into our own models, defensively."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from spotify_api.spotify.mappers import first_track, parse_release_year, to_track

if TYPE_CHECKING:
    from collections.abc import Callable


@pytest.fixture
def raw_track(search_found: dict[str, Any]) -> dict[str, Any]:
    track: dict[str, Any] = search_found["tracks"]["items"][0]
    return track


def test_maps_a_complete_payload(raw_track: dict[str, Any]) -> None:
    track = to_track(raw_track)
    assert track.id == "7tFiyTwD0nx5a1eklYtX2J"
    assert track.name == "Bohemian Rhapsody"
    assert [artist.name for artist in track.artists] == ["Queen"]
    assert track.album.name == "A Night At The Opera (2011 Remaster)"
    assert track.album.release_date == "1975-11-21"
    assert track.album.release_year == 1975
    assert track.duration_ms == 354320
    assert track.explicit is False
    assert track.popularity == 82
    assert track.isrc == "GBUM71029604"
    assert track.preview_url is None
    assert track.external_url == "https://open.spotify.com/track/7tFiyTwD0nx5a1eklYtX2J"
    assert track.uri == "spotify:track:7tFiyTwD0nx5a1eklYtX2J"


@pytest.mark.parametrize(
    ("release_date", "expected"),
    [
        ("1975-11-21", 1975),
        ("1975-11", 1975),
        ("1975", 1975),
        ("", None),
        (None, None),
        ("unknown", None),
        ("75", None),
    ],
)
def test_release_year_is_parsed_from_every_precision_spotify_uses(
    release_date: str | None, expected: int | None
) -> None:
    assert parse_release_year(release_date) == expected


@pytest.mark.parametrize(
    ("mutate", "attribute", "expected"),
    [
        (lambda t: t.pop("external_ids"), "isrc", None),
        (lambda t: t["external_ids"].pop("isrc"), "isrc", None),
        (lambda t: t.pop("popularity"), "popularity", None),
        (lambda t: t.pop("preview_url"), "preview_url", None),
        (lambda t: t.pop("external_urls"), "external_url", None),
        (lambda t: t.pop("artists"), "artists", []),
    ],
)
def test_optional_fields_absent_from_the_payload_do_not_break_the_mapping(
    raw_track: dict[str, Any],
    mutate: Callable[[dict[str, Any]], object],
    attribute: str,
    expected: object,
) -> None:
    mutate(raw_track)
    assert getattr(to_track(raw_track), attribute) == expected


def test_an_artist_missing_its_id_is_still_mapped(raw_track: dict[str, Any]) -> None:
    del raw_track["artists"][0]["id"]
    assert to_track(raw_track).artists[0].id == ""


def test_first_track_returns_the_single_best_match(search_found: dict[str, Any]) -> None:
    raw = first_track(search_found)
    assert raw is not None
    assert raw["id"] == "7tFiyTwD0nx5a1eklYtX2J"


def test_first_track_returns_none_for_an_empty_result_set(search_empty: dict[str, Any]) -> None:
    assert first_track(search_empty) is None


@pytest.mark.parametrize("payload", [{}, {"tracks": {}}, {"tracks": {"items": []}}])
def test_first_track_tolerates_a_malformed_envelope(payload: dict[str, Any]) -> None:
    assert first_track(payload) is None
