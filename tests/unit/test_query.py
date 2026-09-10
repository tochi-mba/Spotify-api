"""Search queries use Spotify's field filters so results are narrow and accurate."""

from __future__ import annotations

import pytest

from spotify_api.models.requests import LookupItem
from spotify_api.spotify.query import build_search_query


def test_a_bare_name_becomes_a_track_filter() -> None:
    assert build_search_query(LookupItem(name="Redbone")) == 'track:"Redbone"'


def test_every_supplied_hint_becomes_its_own_filter() -> None:
    item = LookupItem(
        name="Bohemian Rhapsody", artist="Queen", album="A Night at the Opera", year=1975
    )
    assert build_search_query(item) == (
        'track:"Bohemian Rhapsody" artist:"Queen" album:"A Night at the Opera" year:1975'
    )


def test_absent_hints_are_omitted_entirely() -> None:
    assert build_search_query(LookupItem(name="Redbone", year=2016)) == 'track:"Redbone" year:2016'


def test_embedded_quotes_are_escaped_so_the_filter_cannot_be_broken() -> None:
    query = build_search_query(LookupItem(name='He said "hi"'))
    assert query == 'track:"He said \\"hi\\""'


def test_backslashes_are_escaped_before_quotes() -> None:
    assert build_search_query(LookupItem(name="AC\\DC")) == 'track:"AC\\\\DC"'


@pytest.mark.parametrize("name", ["Björk", "坂本龍一", "Sigur Rós"])
def test_unicode_passes_through_untouched(name: str) -> None:
    assert build_search_query(LookupItem(name=name)) == f'track:"{name}"'


def test_query_is_built_from_validated_input_so_it_is_never_empty() -> None:
    assert build_search_query(LookupItem(name="  x  ")) == 'track:"x"'
