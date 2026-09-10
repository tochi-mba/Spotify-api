"""Search query construction.

Spotify's search endpoint supports *field filters* -- ``artist:``, ``album:``,
``year:`` -- which narrow a search far more effectively than shovelling every
hint into one bag of words. "Bohemian Rhapsody Queen" also matches tribute
covers; ``track:"Bohemian Rhapsody" artist:"Queen"`` does not.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from spotify_api.models.requests import LookupItem

__all__ = ["build_search_query"]


def _quote(value: str) -> str:
    """Wrap ``value`` in double quotes, escaping anything that would break out.

    Backslashes are escaped first; doing it the other way round would double-
    escape the backslashes introduced while escaping the quotes.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def build_search_query(item: LookupItem) -> str:
    """Render ``item`` as a Spotify search string.

    Only the hints the caller actually supplied contribute filters, so an
    unspecified album never narrows the search to nothing.
    """
    filters = [f"track:{_quote(item.name)}"]
    if item.artist is not None:
        filters.append(f"artist:{_quote(item.artist)}")
    if item.album is not None:
        filters.append(f"album:{_quote(item.album)}")
    if item.year is not None:
        filters.append(f"year:{item.year}")
    return " ".join(filters)
