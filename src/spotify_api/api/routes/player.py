"""Playback control.

Every mutation here follows the same shape, and it is the shape the whole
service exists to provide: issue the command, then *confirm* it. Spotify's
``204`` means the command was accepted, which is not the same as audio playing
-- so the response is the player state in which the effect was observed, not a
bare acknowledgement.

With ``?async=true`` the confirmation happens in the background and the caller
polls the job instead. The work is identical either way.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Query, status
from fastapi.responses import JSONResponse

from spotify_api.api.asynchrony import AsyncFlag, run_or_submit
from spotify_api.api.dependencies import (
    ConfirmerDep,
    JobRunnerDep,
    PlayerDep,
    PreferencesDep,
    UserContextDep,
)
from spotify_api.models.player import (
    PlayRequest,
    QueueRequest,
    RepeatRequest,
    SeekRequest,
    ShuffleRequest,
    TransferRequest,
    VolumeRequest,
)
from spotify_api.models.responses import Problem
from spotify_api.models.spotify.player import (
    Devices,
    PlaybackState,
    Queue,
    RecentlyPlayed,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from spotify_api.credentials.models import UserContext
    from spotify_api.jobs.confirm import PlaybackConfirmer, Predicate
    from spotify_api.preferences import Preferences

__all__ = ["router"]

router = APIRouter(prefix="/player", tags=["player"])

MarketQuery = Annotated[
    str | None,
    Query(description="ISO 3166-1 alpha-2 market, affecting track relinking."),
]

_COMMAND_RESPONSES: dict[int | str, dict[str, Any]] = {
    status.HTTP_202_ACCEPTED: {"description": "Accepted for background execution (`?async=true`)."},
    status.HTTP_401_UNAUTHORIZED: {
        "model": Problem,
        "description": "The keyring user token is missing or refused.",
    },
    status.HTTP_403_FORBIDDEN: {
        "model": Problem,
        "description": "Spotify Premium is required for playback control.",
    },
    status.HTTP_409_CONFLICT: {
        "model": Problem,
        "description": "No active device. List /v1/player/devices and name one.",
    },
    status.HTTP_504_GATEWAY_TIMEOUT: {
        "model": Problem,
        "description": (
            "Spotify accepted the command but its effect could not be confirmed. "
            "The last observed player state is in `details.observed`."
        ),
    },
}


async def _command(
    *,
    issue: Callable[[], Awaitable[Predicate]],
    operation: str,
    confirmer: PlaybackConfirmer,
    context: UserContext,
    runner: JobRunnerDep,
    run_async: bool,
    preferences: Preferences,
) -> PlaybackState | JSONResponse:
    """Issue a player command and answer with the state that confirms it.

    The command and its confirmation are one unit of work, which is what lets
    the same coroutine serve both the synchronous and the background path. The
    confirm timeout is captured here, so a running job does not change timeout
    mid-flight if the person later changes the setting.
    """
    timeout_seconds = preferences.confirm_timeout_seconds

    async def work() -> PlaybackState:
        predicate = await issue()
        observed = await confirmer.confirm(
            predicate, context=context, timeout_seconds=timeout_seconds
        )
        return PlaybackState.model_validate(observed)

    return await run_or_submit(
        run_async=run_async,
        operation=operation,
        account_id=context.account_id,
        work=work,
        runner=runner,
    )


# -- reads ------------------------------------------------------------------


@router.get(
    "",
    response_model=PlaybackState | None,
    summary="Get playback state",
    description="What is playing, where and how. Answers `null` when nothing is playing.",
)
async def get_playback_state(
    player: PlayerDep, context: UserContextDep, market: MarketQuery = None
) -> PlaybackState | None:
    """Return the current playback state."""
    state = await player.get_state(context=context, market=market)
    return PlaybackState.model_validate(state) if state is not None else None


@router.get(
    "/devices",
    response_model=Devices,
    summary="List available devices",
    description=(
        "Devices this account can play on. A device must have Spotify open at least "
        "once recently to appear here."
    ),
)
async def get_devices(player: PlayerDep, context: UserContextDep) -> Devices:
    """Return the available devices."""
    return Devices.model_validate(await player.get_devices(context=context))


@router.get(
    "/currently-playing",
    response_model=PlaybackState | None,
    summary="Get the currently playing item",
)
async def get_currently_playing(
    player: PlayerDep, context: UserContextDep, market: MarketQuery = None
) -> PlaybackState | None:
    """Return what is playing right now."""
    state = await player.get_currently_playing(context=context, market=market)
    return PlaybackState.model_validate(state) if state is not None else None


@router.get("/queue", response_model=Queue, summary="Get the playback queue")
async def get_queue(player: PlayerDep, context: UserContextDep) -> Queue:
    """Return what is playing and what is queued behind it."""
    return Queue.model_validate(await player.get_queue(context=context))


@router.get("/recently-played", response_model=RecentlyPlayed, summary="Get recently played tracks")
async def get_recently_played(
    player: PlayerDep,
    context: UserContextDep,
    limit: Annotated[int, Query(ge=1, le=50, description="How many plays to return.")] = 20,
    after: Annotated[
        int | None, Query(description="Unix ms; return plays after this moment.")
    ] = None,
    before: Annotated[
        int | None, Query(description="Unix ms; return plays before this moment.")
    ] = None,
) -> RecentlyPlayed:
    """Return listening history, newest first."""
    return RecentlyPlayed.model_validate(
        await player.get_recently_played(context=context, limit=limit, after=after, before=before)
    )


# -- mutations --------------------------------------------------------------


@router.post(
    "/play",
    response_model=PlaybackState,
    summary="Start or resume playback",
    description=(
        "Starts playback and waits until it is confirmed playing on the device. "
        "Give `uris` for specific tracks, `context_uri` for an album or playlist, or "
        "neither to resume whatever was loaded.\n\n"
        "When `uris` are given the confirmation checks that *that track* is playing, "
        "not merely that something is -- a device already playing something else would "
        "otherwise look like success."
    ),
    responses=_COMMAND_RESPONSES,
)
async def play(
    payload: PlayRequest,
    player: PlayerDep,
    context: UserContextDep,
    confirmer: ConfirmerDep,
    preferences: PreferencesDep,
    runner: JobRunnerDep,
    run_async: AsyncFlag = False,
) -> PlaybackState | JSONResponse:
    """Start or resume playback."""
    return await _command(
        issue=lambda: player.play(
            context=context,
            device_id=payload.device_id,
            context_uri=payload.context_uri,
            uris=payload.uris,
            offset=payload.offset,
            position_ms=payload.position_ms,
        ),
        operation="player.play",
        confirmer=confirmer,
        context=context,
        runner=runner,
        run_async=run_async,
        preferences=preferences,
    )


@router.post(
    "/pause",
    response_model=PlaybackState,
    summary="Pause playback",
    responses=_COMMAND_RESPONSES,
)
async def pause(
    player: PlayerDep,
    context: UserContextDep,
    confirmer: ConfirmerDep,
    preferences: PreferencesDep,
    runner: JobRunnerDep,
    device_id: Annotated[str | None, Query(description="Device to pause.")] = None,
    run_async: AsyncFlag = False,
) -> PlaybackState | JSONResponse:
    """Pause playback."""
    return await _command(
        issue=lambda: player.pause(context=context, device_id=device_id),
        operation="player.pause",
        confirmer=confirmer,
        context=context,
        runner=runner,
        run_async=run_async,
        preferences=preferences,
    )


@router.post(
    "/next",
    response_model=PlaybackState,
    summary="Skip to the next item",
    description=(
        "Confirmed by the loaded item changing, so the current track is read before "
        "the skip is issued."
    ),
    responses=_COMMAND_RESPONSES,
)
async def next_track(
    player: PlayerDep,
    context: UserContextDep,
    confirmer: ConfirmerDep,
    preferences: PreferencesDep,
    runner: JobRunnerDep,
    device_id: Annotated[str | None, Query(description="Device to skip on.")] = None,
    run_async: AsyncFlag = False,
) -> PlaybackState | JSONResponse:
    """Skip forward."""
    return await _command(
        issue=lambda: player.next_track(context=context, device_id=device_id),
        operation="player.next",
        confirmer=confirmer,
        context=context,
        runner=runner,
        run_async=run_async,
        preferences=preferences,
    )


@router.post(
    "/previous",
    response_model=PlaybackState,
    summary="Skip to the previous item",
    responses=_COMMAND_RESPONSES,
)
async def previous_track(
    player: PlayerDep,
    context: UserContextDep,
    confirmer: ConfirmerDep,
    preferences: PreferencesDep,
    runner: JobRunnerDep,
    device_id: Annotated[str | None, Query(description="Device to skip on.")] = None,
    run_async: AsyncFlag = False,
) -> PlaybackState | JSONResponse:
    """Skip backward."""
    return await _command(
        issue=lambda: player.previous_track(context=context, device_id=device_id),
        operation="player.previous",
        confirmer=confirmer,
        context=context,
        runner=runner,
        run_async=run_async,
        preferences=preferences,
    )


@router.post(
    "/seek",
    response_model=PlaybackState,
    summary="Seek within the current item",
    responses=_COMMAND_RESPONSES,
)
async def seek(
    payload: SeekRequest,
    player: PlayerDep,
    context: UserContextDep,
    confirmer: ConfirmerDep,
    preferences: PreferencesDep,
    runner: JobRunnerDep,
    run_async: AsyncFlag = False,
) -> PlaybackState | JSONResponse:
    """Jump to a position."""
    return await _command(
        issue=lambda: player.seek(
            context=context, position_ms=payload.position_ms, device_id=payload.device_id
        ),
        operation="player.seek",
        confirmer=confirmer,
        context=context,
        runner=runner,
        run_async=run_async,
        preferences=preferences,
    )


@router.post(
    "/volume",
    response_model=PlaybackState,
    summary="Set the volume",
    responses=_COMMAND_RESPONSES,
)
async def set_volume(
    payload: VolumeRequest,
    player: PlayerDep,
    context: UserContextDep,
    confirmer: ConfirmerDep,
    preferences: PreferencesDep,
    runner: JobRunnerDep,
    run_async: AsyncFlag = False,
) -> PlaybackState | JSONResponse:
    """Set the device volume."""
    return await _command(
        issue=lambda: player.set_volume(
            context=context,
            volume_percent=payload.volume_percent,
            device_id=payload.device_id,
        ),
        operation="player.volume",
        confirmer=confirmer,
        context=context,
        runner=runner,
        run_async=run_async,
        preferences=preferences,
    )


@router.post(
    "/shuffle",
    response_model=PlaybackState,
    summary="Turn shuffle on or off",
    responses=_COMMAND_RESPONSES,
)
async def set_shuffle(
    payload: ShuffleRequest,
    player: PlayerDep,
    context: UserContextDep,
    confirmer: ConfirmerDep,
    preferences: PreferencesDep,
    runner: JobRunnerDep,
    run_async: AsyncFlag = False,
) -> PlaybackState | JSONResponse:
    """Set shuffle."""
    return await _command(
        issue=lambda: player.set_shuffle(
            context=context, state=payload.state, device_id=payload.device_id
        ),
        operation="player.shuffle",
        confirmer=confirmer,
        context=context,
        runner=runner,
        run_async=run_async,
        preferences=preferences,
    )


@router.post(
    "/repeat",
    response_model=PlaybackState,
    summary="Set the repeat mode",
    responses=_COMMAND_RESPONSES,
)
async def set_repeat(
    payload: RepeatRequest,
    player: PlayerDep,
    context: UserContextDep,
    confirmer: ConfirmerDep,
    preferences: PreferencesDep,
    runner: JobRunnerDep,
    run_async: AsyncFlag = False,
) -> PlaybackState | JSONResponse:
    """Set repeat to off, track or context."""
    return await _command(
        issue=lambda: player.set_repeat(
            context=context, state=payload.state.value, device_id=payload.device_id
        ),
        operation="player.repeat",
        confirmer=confirmer,
        context=context,
        runner=runner,
        run_async=run_async,
        preferences=preferences,
    )


@router.post(
    "/transfer",
    response_model=PlaybackState,
    summary="Move playback to another device",
    description="Confirmed by the named device becoming the active one.",
    responses=_COMMAND_RESPONSES,
)
async def transfer(
    payload: TransferRequest,
    player: PlayerDep,
    context: UserContextDep,
    confirmer: ConfirmerDep,
    preferences: PreferencesDep,
    runner: JobRunnerDep,
    run_async: AsyncFlag = False,
) -> PlaybackState | JSONResponse:
    """Move playback."""
    return await _command(
        issue=lambda: player.transfer(
            context=context, device_id=payload.device_id, play=payload.play
        ),
        operation="player.transfer",
        confirmer=confirmer,
        context=context,
        runner=runner,
        run_async=run_async,
        preferences=preferences,
    )


@router.post(
    "/queue",
    response_model=PlaybackState,
    summary="Add an item to the queue",
    description=(
        "Queues an item behind what is playing. Deliberately does not interrupt "
        "playback, so there is nothing in the player state to confirm against -- "
        "Spotify's acceptance is the signal."
    ),
    responses=_COMMAND_RESPONSES,
)
async def add_to_queue(
    payload: QueueRequest,
    player: PlayerDep,
    context: UserContextDep,
    confirmer: ConfirmerDep,
    preferences: PreferencesDep,
    runner: JobRunnerDep,
    run_async: AsyncFlag = False,
) -> PlaybackState | JSONResponse:
    """Queue an item."""
    return await _command(
        issue=lambda: player.add_to_queue(
            context=context, uri=payload.uri, device_id=payload.device_id
        ),
        operation="player.queue",
        confirmer=confirmer,
        context=context,
        runner=runner,
        run_async=run_async,
        preferences=preferences,
    )
