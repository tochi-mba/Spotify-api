"""The seam between the HTTP layer and Spotify.

Routes depend on these protocols, never on the concrete client. That keeps
Spotify's vocabulary out of the transport layer, lets route tests inject a fake
instead of standing up an HTTP mock, and means a second entity type (albums,
artists, playlists) arrives as a new implementation rather than a rewrite.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Sequence

    from spotify_api.credentials.models import UserContext
    from spotify_api.models.requests import LookupItem
    from spotify_api.models.responses import LookupResult

__all__ = ["TrackResolver"]


@runtime_checkable
class TrackResolver(Protocol):
    """Resolves submitted items into results, one for one, in order."""

    async def resolve(
        self,
        items: Sequence[LookupItem],
        *,
        context: UserContext,
        market: str | None = None,
        default_market: str | None = None,
    ) -> list[LookupResult]:
        """Resolve every item, returning one result per item in input order."""
        ...

    async def check_health(self) -> bool:
        """Report whether the upstream is currently usable."""
        ...
