"""Spotify Client Credentials authentication.

The Client Credentials flow issues an application token that is valid for an
hour. Two properties matter for a batch service:

* the token is cached, because fetching one per lookup would triple our request
  volume and get us rate limited;
* concurrent callers arriving on a cold or expired cache must trigger *one*
  fetch, not one per caller -- a fifty-item batch starting from cold would
  otherwise stampede the token endpoint.

Time is injected rather than read from the module, so expiry is tested directly
instead of being waited for.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

import httpx

from spotify_api.errors import SpotifyAuthError, SpotifyUnavailableError

if TYPE_CHECKING:
    from collections.abc import Callable

    from spotify_api.config import Settings

__all__ = ["ClientCredentialsProvider"]

_logger = logging.getLogger(__name__)

#: Statuses that mean "your credentials are wrong", as opposed to "try later".
_CREDENTIAL_REJECTED = frozenset({400, 401, 403})


class ClientCredentialsProvider:
    """Issues and caches a Spotify application access token."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        settings: Settings,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Store collaborators. No network traffic happens until first use."""
        self._client = client
        self._settings = settings
        self._clock = clock
        self._lock = asyncio.Lock()
        self._token: str | None = None
        self._expires_at: float = 0.0

    @property
    def _token_url(self) -> str:
        return f"{self._settings.spotify_accounts_base_url}/api/token"

    def _is_fresh(self) -> bool:
        """Whether the cached token is usable for a request starting now."""
        if self._token is None:
            return False
        skew = self._settings.token_expiry_skew_seconds
        return self._clock() < self._expires_at - skew

    async def get_token(self, *, force_refresh: bool = False) -> str:
        """Return a usable access token, fetching one only when necessary.

        Args:
            force_refresh: Discard the cached token first. Used when Spotify
                rejects a token we believed was still valid.

        Raises:
            SpotifyAuthError: Spotify rejected our credentials, or returned a
                grant we could not read.
            SpotifyUnavailableError: The token endpoint could not be reached.
        """
        if not force_refresh and self._is_fresh():
            return self._token  # type: ignore[return-value]

        async with self._lock:
            # Another coroutine may have refreshed while we waited for the lock.
            if not force_refresh and self._is_fresh():
                return self._token  # type: ignore[return-value]
            token, expires_in = await self._fetch()
            self._token = token
            self._expires_at = self._clock() + expires_in
            _logger.info("obtained spotify access token", extra={"expires_in": expires_in})
            return token

    async def _fetch(self) -> tuple[str, float]:
        """Perform the token request and return ``(token, lifetime_seconds)``."""
        try:
            response = await self._client.post(
                self._token_url,
                data={"grant_type": "client_credentials"},
                headers={
                    "Authorization": self._settings.basic_auth_header(),
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                timeout=self._settings.request_timeout_seconds,
            )
        except httpx.HTTPError as exc:
            message = "could not reach the Spotify token endpoint"
            raise SpotifyUnavailableError(message, cause=str(exc)) from exc

        if response.status_code in _CREDENTIAL_REJECTED:
            message = "Spotify rejected the configured client credentials"
            raise SpotifyAuthError(message, status_code=response.status_code)

        if response.status_code >= httpx.codes.INTERNAL_SERVER_ERROR:
            message = "the Spotify token endpoint is unavailable"
            raise SpotifyUnavailableError(message, status_code=response.status_code)

        return self._parse_grant(response)

    @staticmethod
    def _parse_grant(response: httpx.Response) -> tuple[str, float]:
        """Read ``access_token`` and ``expires_in`` out of a token grant."""
        try:
            payload = response.json()
            token = payload["access_token"]
            expires_in = float(payload["expires_in"])
        except (ValueError, KeyError, TypeError) as exc:
            message = "the Spotify token response could not be understood"
            raise SpotifyAuthError(message) from exc

        if not token:
            message = "Spotify returned an empty access token"
            raise SpotifyAuthError(message)
        return token, expires_in
