"""The keyring credential provider.

keyring holds the Spotify OAuth grant and refreshes it. We present two things
on every call -- our own service token (which service is asking) and the end
user's short-lived token (whose data we are acting on) -- and keyring answers
with the headers to attach.

Answers are cached until shortly before they expire. Without that, a fifty-item
batch lookup would make fifty keyring round-trips, and a stampede lock keeps a
burst on a cold entry from making fifty concurrent ones.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import time
from typing import TYPE_CHECKING, Any

import httpx

# Credentials package: the adapter that talks to keyring over HTTP. JWKS_PATH is the
# readiness probe, because keyring's own /healthy fails closed on any one person's grant.
from keyring_client import JWKS_PATH

from spotify_api.credentials.models import ResolvedCredential
from spotify_api.errors import (
    CredentialUnavailableError,
    KeyringUnavailableError,
    UserTokenRejectedError,
)
from spotify_api.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

    from spotify_api.config import Settings

__all__ = ["KeyringCredentialProvider"]

_logger = get_logger(__name__)

_UNAUTHORIZED = 401
_FORBIDDEN = 403
_NOT_FOUND = 404
_SERVICE_UNAVAILABLE = 503

#: The service name this credential is filed under in keyring.
SERVICE = "spotify"


class _CacheEntry:
    """A resolved credential and the moment it stops being usable."""

    __slots__ = ("credential", "expires_at")

    def __init__(self, credential: ResolvedCredential, expires_at: float) -> None:
        self.credential = credential
        self.expires_at = expires_at


class KeyringCredentialProvider:
    """Resolves Spotify credentials for a user by asking keyring."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        settings: Settings,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Store collaborators. No traffic happens until first use."""
        self._client = client
        self._settings = settings
        self._clock = clock
        self._cache: dict[str, _CacheEntry] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def resolve(self, *, user_token: str, profile: str) -> ResolvedCredential:
        """Return the headers to attach for this user and profile.

        Raises:
            UserTokenRejectedError: the user's token was refused.
            CredentialUnavailableError: keyring has no usable Spotify grant.
            KeyringUnavailableError: keyring is unreachable or failing.
        """
        key = self._key(user_token=user_token, profile=profile)
        cached = self._fresh(key)
        if cached is not None:
            return cached

        async with self._locks.setdefault(key, asyncio.Lock()):
            # Another coroutine may have resolved while we waited for the lock.
            cached = self._fresh(key)
            if cached is not None:
                return cached

            credential, ttl = await self._fetch(user_token=user_token, profile=profile)
            self._cache[key] = _CacheEntry(credential, self._clock() + ttl)
            return credential

    def invalidate(self, *, user_token: str, profile: str) -> None:
        """Drop the cached credential, so the next resolve asks keyring again."""
        self._cache.pop(self._key(user_token=user_token, profile=profile), None)

    async def check_health(self) -> bool:
        """Whether keyring's published keys are reachable. Never raises.

        The key document rather than keyring's own ``/healthy``, deliberately: that endpoint
        answers 503 whenever any one stored connection anywhere has stopped working, so probing
        it would take this service out of rotation over one person's expired grant. The keys
        are what this service needs from keyring before it can serve anybody.
        """
        try:
            response = await self._client.get(
                f"{self._settings.keyring_base_url}{JWKS_PATH}",
                timeout=self._settings.keyring_timeout_seconds,
            )
        except httpx.HTTPError as exc:
            # The type only: the exception's text carries keyring's URL.
            _logger.warning("keyring_readiness_check_failed", error=type(exc).__name__)
            return False
        return response.is_success

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _key(*, user_token: str, profile: str) -> str:
        """Cache key for a user and profile.

        The token is hashed rather than stored: a cache key ends up in memory
        dumps and debuggers, and the raw token is a bearer credential.
        """
        digest = hashlib.sha256(user_token.encode("utf-8")).hexdigest()
        return f"{profile}:{digest}"

    def _fresh(self, key: str) -> ResolvedCredential | None:
        """Return the cached credential if it is still usable."""
        entry = self._cache.get(key)
        if entry is None or self._clock() >= entry.expires_at:
            return None
        return entry.credential

    async def _fetch(self, *, user_token: str, profile: str) -> tuple[ResolvedCredential, float]:
        """Ask keyring, and return the credential with its cache lifetime."""
        url = f"{self._settings.keyring_base_url}/v1/internal/credentials/{profile}/{SERVICE}"
        try:
            response = await self._client.get(
                url,
                headers={
                    "Authorization": (
                        f"Bearer {self._settings.keyring_service_token.get_secret_value()}"
                    ),
                    "X-Keyring-User-Token": user_token,
                },
                timeout=self._settings.keyring_timeout_seconds,
            )
        except httpx.HTTPError as exc:
            # The type only, and never into the error: the exception's text carries the URL,
            # which names the profile being read.
            _logger.warning("keyring_unreachable", error=type(exc).__name__)
            message = "the keyring credentials service is unreachable"
            raise KeyringUnavailableError(message) from exc

        self._raise_for_status(response)
        return self._parse(response)

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        """Translate keyring's failures into ones that say what to do."""
        status = response.status_code
        if status in (_UNAUTHORIZED, _FORBIDDEN):
            message = "keyring refused the user token"
            raise UserTokenRejectedError(message, upstream_status=status)

        if status == _NOT_FOUND:
            message = "this profile is not connected to Spotify in keyring"
            raise CredentialUnavailableError(message)

        if status == _SERVICE_UNAVAILABLE:
            message = (
                "keyring could not produce a usable Spotify credential -- the grant may "
                "have been revoked, or a refresh failed. Reconnect Spotify in keyring."
            )
            raise CredentialUnavailableError(message, upstream_detail=_detail(response))

        if not response.is_success:
            message = "the keyring credentials service is failing"
            raise KeyringUnavailableError(message, upstream_status=status)

    def _parse(self, response: httpx.Response) -> tuple[ResolvedCredential, float]:
        """Read the credential and work out how long it may be cached."""
        try:
            payload = response.json()
            headers = payload["headers"]
        except (ValueError, KeyError, TypeError) as exc:
            message = "the keyring response could not be understood"
            raise KeyringUnavailableError(message) from exc

        if not isinstance(headers, dict) or not headers:
            message = "the keyring response could not be understood: no headers to attach"
            raise KeyringUnavailableError(message)

        credential = ResolvedCredential(
            headers=headers, query_params=payload.get("query_params") or {}
        )
        return credential, self._ttl_for(payload.get("expires_at"))

    def _ttl_for(self, expires_at: Any) -> float:  # noqa: ANN401
        """How long this credential may be cached, in seconds.

        A credential with no expiry -- an API key, say -- still gets a short
        TTL rather than being held forever, so a revocation at keyring takes
        effect in seconds instead of never.
        """
        default = float(self._settings.credential_cache_default_ttl_seconds)
        if not isinstance(expires_at, str):
            return default
        try:
            moment = dt.datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError:
            _logger.warning("keyring_expires_at_unparseable", raw=expires_at)
            return default

        remaining = (moment - dt.datetime.now(tz=dt.UTC)).total_seconds()
        skew = float(self._settings.credential_cache_skew_seconds)
        return max(0.0, min(remaining - skew, default * 12))


def _detail(response: httpx.Response) -> str | None:
    """Pull keyring's own explanation out of a problem response, if there is one."""
    try:
        payload = response.json()
    except ValueError:
        return None
    detail = payload.get("detail") if isinstance(payload, dict) else None
    return detail if isinstance(detail, str) else None
