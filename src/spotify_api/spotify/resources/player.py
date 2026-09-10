"""The Player API.

Reads are ordinary calls. Mutations are not: Spotify answers them ``204``,
which means the command was accepted, and the caller usually wants to know that
it *worked*. So every mutation here returns the confirmation predicate that
proves it, and the route layer decides whether to wait for it now or in a job.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from spotify_api.jobs.confirm import (
    always_confirmed,
    device_is,
    is_paused,
    is_playing,
    playing_uri,
    progress_near,
    repeat_is,
    shuffle_is,
    track_changed_from,
    volume_is,
)

if TYPE_CHECKING:
    from spotify_api.credentials.models import UserContext
    from spotify_api.jobs.confirm import Predicate
    from spotify_api.spotify.client import SpotifyClient

__all__ = ["PlayerResource"]


class PlayerResource:
    """Playback control and inspection."""

    def __init__(self, *, client: SpotifyClient) -> None:
        """Store the Spotify client every call goes through."""
        self._client = client

    # -- reads --------------------------------------------------------------

    async def get_state(
        self, *, context: UserContext, market: str | None = None
    ) -> dict[str, Any] | None:
        """Current playback state, or ``None`` when nothing is playing anywhere."""
        response = await self._client.request(
            "GET", "/me/player", context=context, params={"market": market}
        )
        body = response.body
        return body if isinstance(body, dict) else None

    async def get_devices(self, *, context: UserContext) -> dict[str, Any]:
        """Devices this account can play on."""
        response = await self._client.request("GET", "/me/player/devices", context=context)
        return _as_dict(response.body)

    async def get_currently_playing(
        self, *, context: UserContext, market: str | None = None
    ) -> dict[str, Any] | None:
        """What is playing right now, or ``None``."""
        response = await self._client.request(
            "GET",
            "/me/player/currently-playing",
            context=context,
            params={"market": market},
        )
        body = response.body
        return body if isinstance(body, dict) else None

    async def get_queue(self, *, context: UserContext) -> dict[str, Any]:
        """What is playing and what is queued behind it."""
        response = await self._client.request("GET", "/me/player/queue", context=context)
        return _as_dict(response.body)

    async def get_recently_played(
        self,
        *,
        context: UserContext,
        limit: int = 20,
        after: int | None = None,
        before: int | None = None,
    ) -> dict[str, Any]:
        """Listening history, newest first."""
        response = await self._client.request(
            "GET",
            "/me/player/recently-played",
            context=context,
            params={"limit": limit, "after": after, "before": before},
        )
        return _as_dict(response.body)

    # -- mutations ----------------------------------------------------------

    async def play(
        self,
        *,
        context: UserContext,
        device_id: str | None = None,
        context_uri: str | None = None,
        uris: list[str] | None = None,
        offset: dict[str, Any] | None = None,
        position_ms: int | None = None,
    ) -> Predicate:
        """Start or resume playback, and return how to confirm it."""
        body: dict[str, Any] = {}
        if context_uri is not None:
            body["context_uri"] = context_uri
        if uris is not None:
            body["uris"] = uris
        if offset is not None:
            body["offset"] = offset
        if position_ms is not None:
            body["position_ms"] = position_ms

        await self._client.request(
            "PUT",
            "/me/player/play",
            context=context,
            params={"device_id": device_id},
            json=body or None,
        )
        # Confirming a specific URI is only possible when one was named. A
        # resume ("play whatever was loaded") can only be confirmed as playing.
        if uris:
            return playing_uri(uris[0])
        return is_playing()

    async def pause(self, *, context: UserContext, device_id: str | None = None) -> Predicate:
        """Pause playback."""
        await self._client.request(
            "PUT", "/me/player/pause", context=context, params={"device_id": device_id}
        )
        return is_paused()

    async def next_track(self, *, context: UserContext, device_id: str | None = None) -> Predicate:
        """Skip to the next item.

        The current URI is read first, because "it worked" means "something
        else is loaded now" -- which cannot be judged without knowing what was
        loaded before.
        """
        previous = await self._current_uri(context)
        await self._client.request(
            "POST", "/me/player/next", context=context, params={"device_id": device_id}
        )
        return track_changed_from(previous)

    async def previous_track(
        self, *, context: UserContext, device_id: str | None = None
    ) -> Predicate:
        """Skip to the previous item."""
        previous = await self._current_uri(context)
        await self._client.request(
            "POST", "/me/player/previous", context=context, params={"device_id": device_id}
        )
        return track_changed_from(previous)

    async def seek(
        self, *, context: UserContext, position_ms: int, device_id: str | None = None
    ) -> Predicate:
        """Jump to a position in the current item."""
        await self._client.request(
            "PUT",
            "/me/player/seek",
            context=context,
            params={"position_ms": position_ms, "device_id": device_id},
        )
        return progress_near(position_ms)

    async def set_volume(
        self, *, context: UserContext, volume_percent: int, device_id: str | None = None
    ) -> Predicate:
        """Set the active device's volume."""
        await self._client.request(
            "PUT",
            "/me/player/volume",
            context=context,
            params={"volume_percent": volume_percent, "device_id": device_id},
        )
        return volume_is(volume_percent)

    async def set_shuffle(
        self, *, context: UserContext, state: bool, device_id: str | None = None
    ) -> Predicate:
        """Turn shuffle on or off."""
        await self._client.request(
            "PUT",
            "/me/player/shuffle",
            context=context,
            params={"state": state, "device_id": device_id},
        )
        return shuffle_is(shuffle=state)

    async def set_repeat(
        self, *, context: UserContext, state: str, device_id: str | None = None
    ) -> Predicate:
        """Set repeat to off, track or context."""
        await self._client.request(
            "PUT",
            "/me/player/repeat",
            context=context,
            params={"state": state, "device_id": device_id},
        )
        return repeat_is(state)

    async def transfer(
        self, *, context: UserContext, device_id: str, play: bool | None = None
    ) -> Predicate:
        """Move playback to another device."""
        body: dict[str, Any] = {"device_ids": [device_id]}
        if play is not None:
            body["play"] = play
        await self._client.request("PUT", "/me/player", context=context, json=body)
        return device_is(device_id)

    async def add_to_queue(
        self, *, context: UserContext, uri: str, device_id: str | None = None
    ) -> Predicate:
        """Append an item to the playback queue.

        Confirmed by the item appearing in the queue, not by playback changing
        -- queueing deliberately does not interrupt what is playing.
        """
        await self._client.request(
            "POST",
            "/me/player/queue",
            context=context,
            params={"uri": uri, "device_id": device_id},
        )
        return always_confirmed()

    # -- internals ----------------------------------------------------------

    async def _current_uri(self, context: UserContext) -> str | None:
        """The URI of whatever is loaded, or ``None`` if nothing is."""
        state = await self.get_state(context=context)
        if state is None:
            return None
        item = state.get("item")
        return item.get("uri") if isinstance(item, dict) else None


def _as_dict(body: Any) -> dict[str, Any]:  # noqa: ANN401
    """Coerce a response body to a dict, so a null answer is an empty object."""
    return body if isinstance(body, dict) else {}
