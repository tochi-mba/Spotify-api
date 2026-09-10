"""Batch resolution.

This is where the service's central promise lives: *partial success*. A batch
of fifty tracks is fifty independent lookups, and one of them failing must cost
the caller nothing but that one result. Every failure is therefore caught per
item and rendered as an error result rather than being allowed to abort the
gather.

Concurrency is bounded. Firing fifty simultaneous searches would earn a 429
immediately; a semaphore keeps us inside Spotify's tolerance while still being
far faster than resolving serially.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from spotify_api.errors import ServiceError
from spotify_api.models.responses import LookupResult, LookupStatus
from spotify_api.spotify.mappers import to_track

if TYPE_CHECKING:
    from collections.abc import Sequence

    from spotify_api.config import Settings
    from spotify_api.models.requests import LookupItem
    from spotify_api.spotify.client import SpotifyClient

__all__ = ["SpotifyTrackResolver"]

_logger = logging.getLogger(__name__)

#: Shown to callers when something we did not anticipate goes wrong. Internal
#: failure detail stays in the logs, where it belongs.
_OPAQUE_ERROR = "an unexpected error occurred while resolving this item"


class SpotifyTrackResolver:
    """Resolves batches of lookup items against Spotify."""

    def __init__(self, *, client: SpotifyClient, settings: Settings) -> None:
        """Store the search client and the settings governing concurrency."""
        self._client = client
        self._settings = settings

    async def resolve(
        self, items: Sequence[LookupItem], *, market: str | None = None
    ) -> list[LookupResult]:
        """Resolve every item, returning one result per item in input order.

        Never raises on behalf of an individual item: an item that cannot be
        resolved comes back with ``status="error"`` and a message.
        """
        if not items:
            return []

        effective_market = market if market is not None else self._settings.default_market
        semaphore = asyncio.Semaphore(self._settings.max_concurrency)

        async def resolve_one(index: int, item: LookupItem) -> LookupResult:
            async with semaphore:
                return await self._resolve_item(index, item, effective_market)

        # gather preserves input order regardless of completion order, which is
        # what lets the caller line results up with what they submitted.
        return await asyncio.gather(*(resolve_one(index, item) for index, item in enumerate(items)))

    async def check_health(self) -> bool:
        """Whether Spotify is currently usable. Never raises."""
        return await self._client.check_health()

    async def _resolve_item(self, index: int, item: LookupItem, market: str | None) -> LookupResult:
        """Resolve a single item, converting any failure into a result."""
        try:
            raw: dict[str, Any] | None = await self._client.search_track(item, market=market)
        except ServiceError as exc:
            _logger.warning(
                "item lookup failed", extra={"item_index": index, "reason": exc.error_type}
            )
            return LookupResult(
                index=index, query=item, status=LookupStatus.ERROR, error=exc.message
            )
        except Exception:
            _logger.exception("unexpected failure resolving item", extra={"item_index": index})
            return LookupResult(
                index=index, query=item, status=LookupStatus.ERROR, error=_OPAQUE_ERROR
            )

        if raw is None:
            return LookupResult(index=index, query=item, status=LookupStatus.NOT_FOUND)
        return LookupResult(index=index, query=item, status=LookupStatus.FOUND, track=to_track(raw))
