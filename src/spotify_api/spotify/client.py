"""The Spotify Web API client.

One generic ``request`` method carries the retry ladder for every endpoint in
the service, so a new resource group is a set of thin callers rather than a
second copy of this logic.

Every rung exists because of a failure Spotify actually produces:

* **401** -- the access token expired. Drop the cached credential, ask keyring
  again (it refreshes the grant) and retry once. A second 401 is a real
  authorisation problem.
* **403** -- not permitted. Most often *not Premium*, which playback requires,
  so that case is named explicitly rather than reported as a generic refusal.
* **404 on a player route** -- no active device. Also named, because "start a
  speaker first" is a different instruction from "that does not exist".
* **429** -- rate limited. ``Retry-After`` is honoured over our own backoff.
* **5xx / timeouts / resets** -- transient. Exponential backoff, capped so one
  slow call cannot hold a batch open.

Sleeping is injected so the ladder is asserted rather than waited out.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx

from spotify_api.errors import (
    NoActiveDeviceError,
    PremiumRequiredError,
    SpotifyAuthError,
    SpotifyRateLimitError,
    SpotifyUnavailableError,
)
from spotify_api.logging import get_logger
from spotify_api.spotify.mappers import first_track
from spotify_api.spotify.query import build_search_query

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from spotify_api.config import Settings
    from spotify_api.credentials.models import UserContext
    from spotify_api.credentials.protocols import CredentialProvider
    from spotify_api.models.requests import LookupItem

__all__ = ["SpotifyClient", "SpotifyResponse"]

_logger = get_logger(__name__)

_UNAUTHORIZED = 401
_FORBIDDEN = 403
_NOT_FOUND = 404
_TOO_MANY_REQUESTS = 429
_NO_CONTENT = 204

#: Marker Spotify uses in 403 bodies when an account is not Premium.
_PREMIUM_MARKERS = ("premium", "player command failed: premium required")

#: Marker Spotify uses in 404 bodies when nothing is playing anywhere.
_NO_DEVICE_MARKERS = ("no active device", "device not found")


@dataclass(frozen=True)
class SpotifyResponse:
    """A successful answer from Spotify.

    Carries the status because it is load-bearing: most player commands answer
    ``204 No Content``, and "accepted with no body" is a different thing from
    "here is your object".
    """

    status_code: int
    body: Any | None = None

    @property
    def is_empty(self) -> bool:
        """Whether Spotify answered without a body."""
        return self.body is None


def _encode(value: Any) -> str:  # noqa: ANN401
    """Render a query value the way Spotify expects it.

    Booleans in particular: Python's ``str(True)`` is ``"True"``, which Spotify
    rejects. Lists become comma-joined, which is how its ``ids`` parameters work.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ",".join(_encode(item) for item in value)
    return str(value)


class SpotifyClient:
    """A resilient, credential-aware wrapper over the Spotify Web API."""

    #: Upper bound on any single backoff sleep, in seconds.
    MAX_BACKOFF_SECONDS = 8.0

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        credentials: CredentialProvider,
        settings: Settings,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """Store collaborators; no traffic is generated until a call is made."""
        self._client = client
        self._credentials = credentials
        self._settings = settings
        self._sleep = sleeper

    async def request(
        self,
        method: str,
        path: str,
        *,
        context: UserContext,
        params: Mapping[str, Any] | None = None,
        json: Any = None,  # noqa: ANN401
    ) -> SpotifyResponse:
        """Call a Spotify endpoint on one user's behalf.

        Args:
            method: HTTP method, e.g. ``"GET"`` or ``"PUT"``.
            path: Path below the API base, e.g. ``"/me/player/play"``.
            context: Whose account to act on.
            params: Query parameters. ``None`` values are dropped, booleans are
                lower-cased and lists comma-joined.
            json: JSON body, when the endpoint takes one.

        Returns:
            The status and decoded body.

        Raises:
            SpotifyAuthError: Spotify refused the credential twice.
            PremiumRequiredError: The account is not Premium.
            NoActiveDeviceError: No device is available to play on.
            SpotifyRateLimitError: Rate limited past the retry budget.
            SpotifyUnavailableError: Unreachable, or failing past the budget.
        """
        url = f"{self._settings.spotify_base_url}{path}"
        query = {key: _encode(value) for key, value in (params or {}).items() if value is not None}
        budget = self._settings.max_retries
        attempt = 0
        reauthenticated = False

        while True:
            credential = await self._credentials.resolve(
                user_token=context.user_token.get_secret_value(), profile=context.profile
            )
            try:
                response = await self._client.request(
                    method,
                    url,
                    params={**query, **credential.query_params},
                    headers=credential.headers,
                    json=json,
                    timeout=self._settings.request_timeout_seconds,
                )
            except httpx.HTTPError as exc:
                if attempt >= budget:
                    # The type only, and never into the error: the exception's text carries the
                    # URL it failed to reach.
                    _logger.warning("spotify_unreachable", error=type(exc).__name__)
                    message = "the Spotify API is unreachable"
                    raise SpotifyUnavailableError(message) from exc
                await self._sleep(self._backoff(attempt))
                attempt += 1
                continue

            if response.status_code == _UNAUTHORIZED and not reauthenticated:
                # The token keyring gave us has expired sooner than advertised.
                # Drop it so keyring refreshes the grant, then try once more.
                # This is a correction, not a retry, so it costs no budget.
                _logger.info("spotify_credential_rejected_resolving_again")
                self._credentials.invalidate(
                    user_token=context.user_token.get_secret_value(), profile=context.profile
                )
                reauthenticated = True
                continue

            if response.status_code == _UNAUTHORIZED:
                message = "Spotify rejected the credential"
                raise SpotifyAuthError(message, upstream_status=_UNAUTHORIZED)

            if response.status_code == _FORBIDDEN:
                self._raise_forbidden(response)

            if response.status_code == _NOT_FOUND:
                self._raise_not_found(response, path)

            if response.status_code == _TOO_MANY_REQUESTS:
                retry_after = self._retry_after(response, attempt)
                if attempt >= budget:
                    message = "Spotify rate limit exceeded and the retry budget is exhausted"
                    raise SpotifyRateLimitError(message, retry_after=retry_after)
                _logger.warning("spotify_rate_limited", retry_after=retry_after)
                await self._sleep(retry_after)
                attempt += 1
                continue

            if response.status_code >= httpx.codes.INTERNAL_SERVER_ERROR:
                if attempt >= budget:
                    message = "the Spotify API is failing"
                    raise SpotifyUnavailableError(message, upstream_status=response.status_code)
                await self._sleep(self._backoff(attempt))
                attempt += 1
                continue

            if response.status_code >= httpx.codes.BAD_REQUEST:
                message = "Spotify rejected the request"
                raise SpotifyUnavailableError(
                    message,
                    upstream_status=response.status_code,
                    upstream_detail=_message_of(response),
                )

            return self._parse(response)

    async def search_track(
        self, item: LookupItem, *, market: str | None, context: UserContext
    ) -> dict[str, Any] | None:
        """Return the best-matching raw track for ``item``, or ``None``."""
        response = await self.request(
            "GET",
            "/search",
            context=context,
            params={
                "q": build_search_query(item),
                "type": "track",
                "limit": 1,
                "market": market,
            },
        )
        payload = response.body
        if not isinstance(payload, dict):
            message = "the Spotify search response was not a JSON object"
            raise SpotifyUnavailableError(message)
        return first_track(payload)

    async def check_health(self) -> bool:
        """Whether the credential source is reachable. Never raises."""
        return await self._credentials.check_health()

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _raise_forbidden(response: httpx.Response) -> None:
        """Distinguish "you are not Premium" from every other refusal."""
        detail = (_message_of(response) or "").lower()
        if any(marker in detail for marker in _PREMIUM_MARKERS):
            message = (
                "this action requires a Spotify Premium account; playback control is "
                "not available on free accounts"
            )
            raise PremiumRequiredError(message)
        message = "Spotify refused the request"
        raise SpotifyAuthError(
            message, upstream_status=_FORBIDDEN, upstream_detail=_message_of(response)
        )

    @staticmethod
    def _raise_not_found(response: httpx.Response, path: str) -> None:
        """Distinguish "no device is awake" from "that object does not exist"."""
        detail = (_message_of(response) or "").lower()
        if path.startswith("/me/player") or any(m in detail for m in _NO_DEVICE_MARKERS):
            message = (
                "no active Spotify device was found; open Spotify on a device, or pass "
                "device_id explicitly"
            )
            raise NoActiveDeviceError(message)
        message = "Spotify has no such resource"
        raise SpotifyUnavailableError(
            message, upstream_status=_NOT_FOUND, upstream_detail=_message_of(response)
        )

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
                _logger.warning("spotify_retry_after_unparseable", retry_after_raw=raw)
        return self._backoff(attempt)

    @staticmethod
    def _parse(response: httpx.Response) -> SpotifyResponse:
        """Decode a successful response, tolerating an empty body."""
        if response.status_code == _NO_CONTENT or not response.content:
            return SpotifyResponse(status_code=response.status_code)
        try:
            return SpotifyResponse(status_code=response.status_code, body=response.json())
        except ValueError as exc:
            message = "the Spotify response could not be understood"
            raise SpotifyUnavailableError(message) from exc


def _message_of(response: httpx.Response) -> str | None:
    """Pull Spotify's own error message out of a failure body."""
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        return message if isinstance(message, str) else None
    return error if isinstance(error, str) else None
