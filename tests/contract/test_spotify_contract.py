"""Assumptions this service makes about Spotify's response shape.

These tests exist to fail loudly if Spotify changes its schema, or if someone
re-records a fixture without noticing what it broke. They assert the *shape* we
depend on -- not the values, which are free to drift.

To refresh a fixture, see docs/testing.md.
"""

from __future__ import annotations

from typing import Any

import pytest

from spotify_api.spotify.mappers import first_track, to_track

#: Keys the mapper reads off a track object. Losing one silently degrades the
#: response instead of failing, which is exactly what this guards against.
REQUIRED_TRACK_KEYS = frozenset(
    {"id", "name", "artists", "album", "duration_ms", "explicit", "uri"}
)
OPTIONAL_TRACK_KEYS = frozenset({"popularity", "preview_url", "external_ids", "external_urls"})


@pytest.fixture
def raw(search_found: dict[str, Any]) -> dict[str, Any]:
    track = first_track(search_found)
    assert track is not None
    return track


def test_a_search_response_nests_items_under_tracks(search_found: dict[str, Any]) -> None:
    assert isinstance(search_found["tracks"]["items"], list)


def test_an_empty_search_still_carries_the_envelope(search_empty: dict[str, Any]) -> None:
    assert search_empty["tracks"]["items"] == []
    assert search_empty["tracks"]["total"] == 0


@pytest.mark.parametrize("key", sorted(REQUIRED_TRACK_KEYS))
def test_every_key_the_mapper_requires_is_present(raw: dict[str, Any], key: str) -> None:
    assert key in raw


@pytest.mark.parametrize("key", sorted(OPTIONAL_TRACK_KEYS))
def test_every_optional_key_is_still_the_shape_we_expect(raw: dict[str, Any], key: str) -> None:
    if key in raw:
        assert raw[key] is None or isinstance(raw[key], dict | str | int)


def test_artists_are_objects_carrying_an_id_and_a_name(raw: dict[str, Any]) -> None:
    assert raw["artists"]
    for artist in raw["artists"]:
        assert isinstance(artist["id"], str)
        assert isinstance(artist["name"], str)


def test_the_album_carries_a_release_date_and_its_precision(raw: dict[str, Any]) -> None:
    album = raw["album"]
    assert isinstance(album["release_date"], str)
    assert album["release_date_precision"] in {"day", "month", "year"}


def test_the_isrc_lives_under_external_ids(raw: dict[str, Any]) -> None:
    assert isinstance(raw["external_ids"]["isrc"], str)


def test_the_public_url_lives_under_external_urls_spotify(raw: dict[str, Any]) -> None:
    assert raw["external_urls"]["spotify"].startswith("https://open.spotify.com/track/")


def test_the_uri_uses_the_spotify_track_scheme(raw: dict[str, Any]) -> None:
    assert raw["uri"].startswith("spotify:track:")


def test_the_recorded_fixture_maps_without_loss(raw: dict[str, Any]) -> None:
    # The end-to-end assertion: if this fails, the fixture and the mapper have
    # drifted apart and one of them is wrong.
    track = to_track(raw)
    assert track.id == raw["id"]
    assert track.name == raw["name"]
    assert track.uri == raw["uri"]
    assert track.duration_ms == raw["duration_ms"]
    assert len(track.artists) == len(raw["artists"])
    assert track.isrc == raw["external_ids"]["isrc"]
    assert track.album.release_year == int(raw["album"]["release_date"][:4])
