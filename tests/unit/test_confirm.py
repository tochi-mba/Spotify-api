"""A playback command is not done until the effect is observable."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from spotify_api.credentials.models import UserContext
from spotify_api.errors import ConfirmationTimeoutError, NoActiveDeviceError
from spotify_api.jobs.confirm import (
    PlaybackConfirmer,
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
from spotify_api.spotify.client import SpotifyResponse
from tests.factories import make_settings

if TYPE_CHECKING:
    from collections.abc import Callable

CONTEXT = UserContext(account_id="account-a", user_token=SecretStr("t"), profile="personal")

TRACK = "spotify:track:7tFiyTwD0nx5a1eklYtX2J"
OTHER = "spotify:track:0000000000000000000000"


def state(**overrides: Any) -> dict[str, Any]:
    """A plausible GET /me/player body."""
    base: dict[str, Any] = {
        "is_playing": True,
        "progress_ms": 1000,
        "shuffle_state": False,
        "repeat_state": "off",
        "device": {"id": "device-1", "volume_percent": 50, "is_active": True},
        "item": {"uri": TRACK, "name": "Bohemian Rhapsody"},
    }
    base.update(overrides)
    return base


# -- the predicates ---------------------------------------------------------


@pytest.mark.parametrize(
    ("predicate", "player", "expected"),
    [
        (playing_uri(TRACK), state(), True),
        (playing_uri(TRACK), state(item={"uri": OTHER}), False),
        (playing_uri(TRACK), state(is_playing=False), False),
        (playing_uri(TRACK), state(item=None), False),
        (playing_uri(TRACK), {}, False),
        (is_playing(), state(), True),
        (is_playing(), state(is_playing=False), False),
        (is_paused(), state(is_playing=False), True),
        (is_paused(), state(), False),
        (track_changed_from(TRACK), state(item={"uri": OTHER}), True),
        (track_changed_from(TRACK), state(), False),
        (track_changed_from(None), state(), True),
        (progress_near(1000, tolerance_ms=500), state(), True),
        (progress_near(5000, tolerance_ms=500), state(), False),
        (progress_near(1200, tolerance_ms=500), state(), True),
        (progress_near(1000, tolerance_ms=500), state(progress_ms=None), False),
        (volume_is(50), state(), True),
        (volume_is(80), state(), False),
        (volume_is(50), state(device={}), False),
        (shuffle_is(shuffle=False), state(), True),
        (shuffle_is(shuffle=True), state(), False),
        (repeat_is("off"), state(), True),
        (repeat_is("track"), state(), False),
        (device_is("device-1"), state(), True),
        (device_is("device-2"), state(), False),
    ],
)
def test_predicates_read_the_player_state(
    predicate: Callable[[dict[str, Any]], bool], player: dict[str, Any], expected: bool
) -> None:
    assert predicate(player) is expected


# -- the confirmer ----------------------------------------------------------


class FakePlayer:
    """Serves a scripted sequence of GET /me/player responses."""

    def __init__(self, *states: Any) -> None:
        self.states = list(states)
        self.calls = 0

    async def request(self, method: str, path: str, **_: Any) -> SpotifyResponse:
        assert method == "GET"
        assert path == "/me/player"
        self.calls += 1
        value = self.states[min(self.calls - 1, len(self.states) - 1)]
        if isinstance(value, Exception):
            raise value
        if value is None:
            return SpotifyResponse(status_code=204)
        return SpotifyResponse(status_code=200, body=value)


class RecordingSleeper:
    def __init__(self) -> None:
        self.slept: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.slept.append(seconds)


class FakeClock:
    def __init__(self, sleeper: RecordingSleeper) -> None:
        self._sleeper = sleeper

    def __call__(self) -> float:
        # Time only passes when the confirmer sleeps, which makes a 15-second
        # timeout assertable in microseconds.
        return sum(self._sleeper.slept)


def build_confirmer(player: FakePlayer, **overrides: Any) -> tuple[PlaybackConfirmer, Any]:
    sleeper = RecordingSleeper()
    confirmer = PlaybackConfirmer(
        client=player,  # type: ignore[arg-type]
        settings=make_settings(**overrides),
        sleeper=sleeper,
        clock=FakeClock(sleeper),
    )
    return confirmer, sleeper


async def test_it_returns_as_soon_as_the_predicate_holds() -> None:
    player = FakePlayer(state())
    confirmer, sleeper = build_confirmer(player)

    observed = await confirmer.confirm(playing_uri(TRACK), context=CONTEXT)

    assert observed["item"]["uri"] == TRACK
    assert player.calls == 1
    assert sleeper.slept == []


async def test_it_keeps_polling_until_the_effect_appears() -> None:
    # A device takes a moment to wake up and start the right track.
    player = FakePlayer(state(is_playing=False), state(item={"uri": OTHER}), state())
    confirmer, sleeper = build_confirmer(player, confirm_poll_interval_seconds=0.5)

    await confirmer.confirm(playing_uri(TRACK), context=CONTEXT)

    assert player.calls == 3
    assert sleeper.slept == [0.5, 0.5]


async def test_an_explicit_timeout_is_used_instead_of_the_configured_one() -> None:
    player = FakePlayer(state(is_playing=False))
    confirmer, _ = build_confirmer(
        player, confirm_timeout_seconds=10, confirm_poll_interval_seconds=0.5
    )

    with pytest.raises(ConfirmationTimeoutError) as excinfo:
        await confirmer.confirm(playing_uri(TRACK), context=CONTEXT, timeout_seconds=1)

    assert "1s" in excinfo.value.message
    assert "10s" not in excinfo.value.message


async def test_it_gives_up_after_the_timeout_and_says_what_it_saw() -> None:
    player = FakePlayer(state(is_playing=False))
    confirmer, _ = build_confirmer(
        player, confirm_timeout_seconds=2, confirm_poll_interval_seconds=0.5
    )

    with pytest.raises(ConfirmationTimeoutError) as excinfo:
        await confirmer.confirm(playing_uri(TRACK), context=CONTEXT)

    # The last observed state is attached, so a caller can see *why*.
    assert excinfo.value.details["observed"]["is_playing"] is False


async def test_a_204_from_the_player_means_nothing_is_playing_anywhere() -> None:
    # Spotify answers 204 when there is no active device at all.
    player = FakePlayer(None, None, state())
    confirmer, _ = build_confirmer(player, confirm_poll_interval_seconds=0.1)

    await confirmer.confirm(playing_uri(TRACK), context=CONTEXT)
    assert player.calls == 3


async def test_a_persistent_204_times_out_as_no_active_device() -> None:
    player = FakePlayer(None)
    confirmer, _ = build_confirmer(
        player, confirm_timeout_seconds=1, confirm_poll_interval_seconds=0.5
    )

    with pytest.raises(NoActiveDeviceError):
        await confirmer.confirm(playing_uri(TRACK), context=CONTEXT)


async def test_a_transient_upstream_failure_does_not_end_the_wait() -> None:
    player = FakePlayer(NoActiveDeviceError("no device yet"), state())
    confirmer, _ = build_confirmer(player, confirm_poll_interval_seconds=0.1)

    await confirmer.confirm(playing_uri(TRACK), context=CONTEXT)
    assert player.calls == 2


async def test_each_poll_is_recorded_against_the_job() -> None:
    class Recorder:
        def __init__(self) -> None:
            self.attempts = 0

        async def record_attempt(self, job_id: str) -> None:
            assert job_id == "job-1"
            self.attempts += 1

    player = FakePlayer(state(is_playing=False), state())
    confirmer, _ = build_confirmer(player, confirm_poll_interval_seconds=0.1)
    recorder = Recorder()

    await confirmer.confirm(
        playing_uri(TRACK),
        context=CONTEXT,
        store=recorder,  # type: ignore[arg-type]
        job_id="job-1",
    )
    assert recorder.attempts == 2
