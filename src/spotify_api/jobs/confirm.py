"""Confirming that a playback command actually took effect.

Spotify answers most player commands with ``204 No Content``, which means *the
command was accepted* -- not *audio is playing*. The gap between those two is
where the real failures live: a device that was asleep, a command silently
ignored, a different track already playing.

So after issuing a command we poll ``GET /me/player`` until what we asked for is
observably true, or until we run out of patience. Time and sleep are injected,
so a fifteen-second timeout is asserted in microseconds rather than waited out.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any, TypeAlias

from spotify_api.errors import (
    ConfirmationTimeoutError,
    NoActiveDeviceError,
    ServiceError,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from spotify_api.config import Settings
    from spotify_api.credentials.models import UserContext
    from spotify_api.jobs.protocols import JobStore
    from spotify_api.spotify.client import SpotifyClient

__all__ = [
    "PlaybackConfirmer",
    "Predicate",
    "device_is",
    "is_paused",
    "is_playing",
    "playing_uri",
    "progress_near",
    "repeat_is",
    "shuffle_is",
    "track_changed_from",
    "volume_is",
]

_logger = logging.getLogger(__name__)

#: A question asked of the player state: "is what I wanted true yet?"
Predicate: TypeAlias = "Callable[[dict[str, Any]], bool]"

#: Seeking is not sample-accurate, and the track keeps playing while we poll.
DEFAULT_SEEK_TOLERANCE_MS = 3000


def _current_uri(player: dict[str, Any]) -> str | None:
    """The URI of whatever is loaded, if anything is."""
    item = player.get("item")
    return item.get("uri") if isinstance(item, dict) else None


def playing_uri(uri: str) -> Predicate:
    """Playing, *and* playing the thing that was asked for.

    Both halves matter: a device already playing something else would satisfy
    "is playing" immediately, which is a false confirmation.
    """

    def predicate(player: dict[str, Any]) -> bool:
        return bool(player.get("is_playing")) and _current_uri(player) == uri

    return predicate


def is_playing() -> Predicate:
    """Playing anything at all -- for a resume with no track specified."""
    return lambda player: bool(player.get("is_playing"))


def is_paused() -> Predicate:
    """Not playing."""
    return lambda player: not player.get("is_playing")


def track_changed_from(uri: str | None) -> Predicate:
    """Something other than ``uri`` is loaded -- for next and previous."""
    return lambda player: _current_uri(player) != uri


def progress_near(target_ms: int, *, tolerance_ms: int = DEFAULT_SEEK_TOLERANCE_MS) -> Predicate:
    """Playback position is close to ``target_ms``."""

    def predicate(player: dict[str, Any]) -> bool:
        progress = player.get("progress_ms")
        if not isinstance(progress, int):
            return False
        return abs(progress - target_ms) <= tolerance_ms

    return predicate


def volume_is(percent: int) -> Predicate:
    """The active device is at the requested volume."""

    def predicate(player: dict[str, Any]) -> bool:
        device = player.get("device")
        return isinstance(device, dict) and device.get("volume_percent") == percent

    return predicate


def shuffle_is(*, shuffle: bool) -> Predicate:
    """Shuffle is in the requested state."""
    return lambda player: bool(player.get("shuffle_state")) is shuffle


def repeat_is(state: str) -> Predicate:
    """Repeat is in the requested state: off, track or context."""
    return lambda player: player.get("repeat_state") == state


def device_is(device_id: str) -> Predicate:
    """Playback has moved to the requested device."""

    def predicate(player: dict[str, Any]) -> bool:
        device = player.get("device")
        return isinstance(device, dict) and device.get("id") == device_id

    return predicate


class PlaybackConfirmer:
    """Polls the player until a command's effect is observable."""

    def __init__(
        self,
        *,
        client: SpotifyClient,
        settings: Settings,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Store collaborators. Time and sleep are injected for testability."""
        self._client = client
        self._settings = settings
        self._sleep = sleeper
        self._clock = clock

    async def confirm(
        self,
        predicate: Predicate,
        *,
        context: UserContext,
        store: JobStore | None = None,
        job_id: str | None = None,
    ) -> dict[str, Any]:
        """Poll until ``predicate`` holds, and return the state that satisfied it.

        Args:
            predicate: The question asked of each observed player state.
            context: Whose player to watch.
            store: Job store to record poll attempts against, when running as a job.
            job_id: The job the attempts belong to.

        Returns:
            The player state in which the predicate first held.

        Raises:
            NoActiveDeviceError: Nothing was playing anywhere for the whole wait.
            ConfirmationTimeoutError: The command was accepted but never took effect.
        """
        deadline = self._clock() + self._settings.confirm_timeout_seconds
        interval = self._settings.confirm_poll_interval_seconds
        observed: dict[str, Any] | None = None
        saw_a_device = False

        while True:
            if store is not None and job_id is not None:
                await store.record_attempt(job_id)

            player = await self._poll(context)
            if player is not None:
                saw_a_device = True
                observed = player
                if predicate(player):
                    return player

            if self._clock() >= deadline:
                break
            await self._sleep(interval)

        if not saw_a_device:
            message = (
                "the command was accepted but nothing is playing on any device; open "
                "Spotify on a device, or pass device_id explicitly"
            )
            raise NoActiveDeviceError(message)

        message = (
            "the command was accepted but its effect could not be confirmed within "
            f"{self._settings.confirm_timeout_seconds:g}s"
        )
        raise ConfirmationTimeoutError(message, observed=observed)

    async def _poll(self, context: UserContext) -> dict[str, Any] | None:
        """Read the player once. ``None`` means nothing is playing anywhere.

        A failure here does not end the wait: a device coming out of sleep can
        refuse a poll or two before it settles, and giving up on the first one
        would report a failure that was about to become a success.
        """
        try:
            response = await self._client.request("GET", "/me/player", context=context)
        except ServiceError as exc:
            _logger.info("player poll failed; still waiting", extra={"reason": exc.error_type})
            return None
        body = response.body
        return body if isinstance(body, dict) else None
