"""Translation from raw Spotify JSON into this service's models.

Mapping is deliberately defensive. Spotify's schema is stable but not
guaranteed, fields are genuinely optional in places (``preview_url`` is often
null, ``external_ids.isrc`` is absent for some catalogue entries), and a single
missing key must never turn a whole batch into a 500. Anything we cannot read
becomes ``None`` rather than an exception.
"""

from __future__ import annotations

from typing import Any

from spotify_api.models.responses import Album, Artist, Track

__all__ = ["first_track", "parse_release_year", "to_track"]

#: Spotify reports release dates with day, month or year precision.
_YEAR_LENGTH = 4


def parse_release_year(release_date: str | None) -> int | None:
    """Extract the year from a Spotify release date.

    Spotify's ``release_date_precision`` may be ``day``, ``month`` or ``year``,
    giving ``1975-11-21``, ``1975-11`` or ``1975``. Anything else yields
    ``None`` rather than a guess.
    """
    if not release_date:
        return None
    head = release_date.split("-", 1)[0]
    if len(head) != _YEAR_LENGTH or not head.isdigit():
        return None
    return int(head)


def _to_artist(raw: dict[str, Any]) -> Artist:
    return Artist(id=raw.get("id") or "", name=raw.get("name") or "")


def _to_album(raw: dict[str, Any]) -> Album:
    release_date = raw.get("release_date")
    return Album(
        id=raw.get("id") or "",
        name=raw.get("name") or "",
        release_date=release_date,
        release_year=parse_release_year(release_date),
    )


def to_track(raw: dict[str, Any]) -> Track:
    """Map one raw Spotify track object onto a :class:`Track`."""
    external_ids = raw.get("external_ids") or {}
    external_urls = raw.get("external_urls") or {}
    return Track(
        id=raw.get("id") or "",
        name=raw.get("name") or "",
        artists=[_to_artist(artist) for artist in raw.get("artists") or []],
        album=_to_album(raw.get("album") or {}),
        duration_ms=raw.get("duration_ms") or 0,
        explicit=bool(raw.get("explicit", False)),
        popularity=raw.get("popularity"),
        isrc=external_ids.get("isrc"),
        preview_url=raw.get("preview_url"),
        external_url=external_urls.get("spotify"),
        uri=raw.get("uri") or "",
    )


def first_track(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Return the best match from a search response, or ``None``.

    We ask Spotify for a single result, so "first" is "best" by Spotify's own
    relevance ranking.
    """
    items = (payload.get("tracks") or {}).get("items") or []
    if not items:
        return None
    first: dict[str, Any] = items[0]
    return first
