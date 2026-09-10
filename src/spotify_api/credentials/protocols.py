"""The credential seam.

The Spotify client depends on this, never on keyring's HTTP shape, so the
credential source can be swapped without touching the adapter.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from spotify_api.credentials.models import ResolvedCredential

__all__ = ["CredentialProvider"]


@runtime_checkable
class CredentialProvider(Protocol):
    """Supplies the headers to attach for one user's Spotify requests."""

    async def resolve(self, *, user_token: str, profile: str) -> ResolvedCredential:
        """Return what to attach for this user and profile."""
        ...

    def invalidate(self, *, user_token: str, profile: str) -> None:
        """Drop any cached credential, so the next resolve asks again."""
        ...

    async def check_health(self) -> bool:
        """Report whether the credential source is reachable. Never raises."""
        ...
