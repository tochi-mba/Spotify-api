"""The Spotify Web API search client.

Everything here exists because of a failure Spotify actually produces:

* **401** -- our cached token expired earlier than we believed. Refresh once and
  retry; a second 401 means the credentials themselves are wrong.
* **403** -- the credentials are not permitted to do this. Retrying cannot help.
* **429** -- rate limited. Spotify tells us how long to wait in ``Retry-After``
  and that instruction is honoured in preference to our own backoff.
* **5xx / timeouts / resets** -- transient. Retried with exponential backoff,
  capped so that one slow item cannot hold a batch open indefinitely.

Sleeping is injected so the retry ladder is asserted rather than waited out.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

import httpx

from spotify_api.errors import (
    SpotifyAuthError,
    SpotifyRateLimitError,
    SpotifyUnavailableError,
)
from spotify_api.spotify.mappers import first_track
from spotify_api.spotify.query import build_search_query

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from spotify_api.config import Settings
    from spotify_api.models.requests import LookupItem
    from spotify_api.spotify.protocols import TokenProvider

__all__ = ["SpotifyClient"]

_logger = logging.getLogger(__name__)

_UNAUTHORIZED = 401
_FORBIDDEN = 403
_TOO_MANY_REQUESTS = 429


class SpotifyClient:
    """A thin, resilient wrapper over Spotify's ``/search`` endpoint."""

    #: Upper bound on any single backoff sleep, in seconds. Without it a
    #: generous retry budget could park a request for minutes.
    MAX_BACKOFF_SECONDS = 8.0

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        token_provider: TokenProvider,
        settings: Settings,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """Store collaborators; no traffic is generated until a search runs."""
        self._client = client
        self._tokens = token_provider
        self._settings = settings
        self._sleep = sleeper

    async def search_track(self, item: LookupItem, *, market: str | None) -> dict[str, Any] | None:
        """Return the best-matching raw track for ``item``, or ``None``.

        Args:
            item: The validated lookup item to search for.
            market: ISO 3166-1 alpha-2 market, or ``None`` to leave it to
                Spotify.

        Returns:
            Spotify's raw track object, or ``None`` when nothing matched.

        Raises:
            SpotifyAuthError: Credentials were rejected.
            SpotifyRateLimitError: Rate limited past the retry budget.
            SpotifyUnavailableError: Unreachable, or failing past the budget.
        """
        params: dict[str, str] = {
            "q": build_search_query(item),
            "type": "track",
            "limit": "1",
        }
        if market is not None:
            params["market"] = market

        payload = await self._get_with_retries("/search", params=params)
        return first_track(payload)

    async def check_health(self) -> bool:
        """Whether a token can currently be obtained. Never raises."""
        try:
            await self._tokens.get_token()
        except (SpotifyAuthError, SpotifyUnavailableError):
            _logger.warning("spotify readiness check failed", exc_info=True)
            return False
        return True

    # -- internals ----------------------------------------------------------

    async def _get_with_retries(self, path: str, *, params: dict[str, str]) -> dict[str, Any]:
        """Issue a GET, applying the auth-refresh and backoff ladders.

        Re-authentication is deliberately kept out of the retry budget: a stale
        token is a correction to make, not a failed call to repeat, and it can
        happen at most once per request.
        """
        url = f"{self._settings.spotify_api_base_url}{path}"
        budget = self._settings.max_retries
        attempt = 0
        refreshed = False
        last_retry_after: float | None = None

        while True:
            token = await self._tokens.get_token(force_refresh=refreshed)
            try:
                response = await self._client.get(
                    url,
                    params=params,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=self._settings.request_timeout_seconds,
                )
            except httpx.HTTPError as exc:
                if attempt >= budget:
                    message = "the Spotify API is unreachable"
                    raise SpotifyUnavailableError(message, cause=str(exc)) from exc
                await self._sleep(self._backoff(attempt))
                attempt += 1
                continue

            if response.status_code == _UNAUTHORIZED and not refreshed:
                _logger.info("spotify rejected the access token; refreshing once")
                refreshed = True
                continue

            if response.status_code in (_UNAUTHORIZED, _FORBIDDEN):
                message = "Spotify rejected the request as unauthorised"
                raise SpotifyAuthError(message, status_code=response.status_code)

            if response.status_code == _TOO_MANY_REQUESTS:
                last_retry_after = self._retry_after(response, attempt)
                if attempt >= budget:
                    message = "Spotify rate limit exceeded and the retry budget is exhausted"
                    raise SpotifyRateLimitError(message, retry_after=last_retry_after)
                _logger.warning(
                    "spotify rate limited the request", extra={"retry_after": last_retry_after}
                )
                await self._sleep(last_retry_after)
                attempt += 1
                continue

            if response.status_code >= httpx.codes.INTERNAL_SERVER_ERROR:
                if attempt >= budget:
                    message = "the Spotify API is failing"
                    raise SpotifyUnavailableError(message, status_code=response.status_code)
                await self._sleep(self._backoff(attempt))
                attempt += 1
                continue

            if response.status_code >= httpx.codes.BAD_REQUEST:
                message = "Spotify returned an unexpected client error"
                raise SpotifyUnavailableError(message, status_code=response.status_code)

            return self._parse(response)

    def _backoff(self, attempt: int) -> float:
        """Exponential backoff for ``attempt``, capped."""
        # 2.0 rather than 2: mypy types int**int as Any, since a negative
        # exponent would produce a float.
        base = self._settings.retry_backoff_base_seconds * (2.0**attempt)
        return min(base, self.MAX_BACKOFF_SECONDS)

    def _retry_after(self, response: httpx.Response, attempt: int) -> float:
        """Honour Spotify's ``Retry-After``, falling back to our own backoff."""
        raw = response.headers.get("Retry-After")
        if raw is not None:
            try:
                return min(float(raw), self.MAX_BACKOFF_SECONDS)
            except ValueError:
                _logger.warning("could not parse Retry-After", extra={"retry_after_raw": raw})
        return self._backoff(attempt)

    @staticmethod
    def _parse(response: httpx.Response) -> dict[str, Any]:
        """Decode a successful search response."""
        try:
            decoded: object = response.json()
        except ValueError as exc:
            message = "the Spotify search response could not be understood"
            raise SpotifyUnavailableError(message) from exc
        if not isinstance(decoded, dict):
            message = "the Spotify search response was not a JSON object"
            raise SpotifyUnavailableError(message)
        return decoded
