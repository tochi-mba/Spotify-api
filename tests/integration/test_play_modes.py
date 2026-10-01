"""A new play takes the person's shuffle and repeat, unless the request says otherwise.

The bug, named: `spotify.shuffle_on_play` and `spotify.repeat_mode` could be set and read
back, and changed nothing. Every play left shuffle and repeat as the device last had them,
whatever the person had chosen.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
import pytest
from settings_client.testing import FakeSettingsClient

from spotify_api.app import create_app
from spotify_api.jobs.confirm import all_of, is_playing, shuffle_is
from spotify_api.models.player import PlayRequest
from spotify_api.models.spotify.player import RepeatState
from spotify_api.preferences import Preferences, build_preference_source
from tests.factories import make_settings, problem_type
from tests.integration.test_player_routes import (
    SPOTIFY_URL,
    TRACK,
    USER_TOKEN,
    FakeSpotify,
    player_state,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

ALBUM = "spotify:album:1GbtB4zTqAsyfZEsm1RZfx"
PLAYING = {"is_playing": True, "item": {"uri": TRACK}}


def chose(**values: Any) -> Preferences:
    return Preferences(
        default_market=None,
        max_batch_size=1,
        confirm_timeout_seconds=1.0,
        default_profile="personal",
        **values,
    )


def sent(spotify: FakeSpotify) -> list[tuple[str, dict[str, str]]]:
    """Each command Spotify was sent, as its path and its query."""
    return [(path, params) for _method, path, params in spotify.commands]


@pytest.fixture
def spotify() -> FakeSpotify:
    return FakeSpotify()


@pytest.fixture
def chosen() -> FakeSettingsClient:
    return FakeSettingsClient()


@pytest.fixture
async def client(
    spotify: FakeSpotify, chosen: FakeSettingsClient
) -> AsyncIterator[httpx.AsyncClient]:
    settings = make_settings(
        keyring_base_url="https://keyring.test",
        spotify_base_url=SPOTIFY_URL,
        confirm_poll_interval_seconds=0.001,
        confirm_timeout_seconds=0.05,
    )
    app = create_app(
        settings=settings,
        transport=httpx.MockTransport(spotify),
        preferences=build_preference_source(settings, client=chosen),
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"Authorization": f"Bearer {USER_TOKEN}"},
        ) as http_client:
            yield http_client


# -- what one play is set to ----------------------------------------------------------------


def test_a_play_names_what_to_play_or_resumes() -> None:
    assert PlayRequest(uris=[TRACK]).starts_something
    assert PlayRequest(context_uri=ALBUM).starts_something
    assert not PlayRequest().starts_something
    assert not PlayRequest(uris=[]).starts_something


def test_a_request_that_says_wins_over_the_setting() -> None:
    preferences = chose(shuffle_on_play=True, repeat_mode="context")
    play = PlayRequest(uris=[TRACK], shuffle=False, repeat=RepeatState.TRACK)

    assert preferences.shuffle_for(play) is False
    assert preferences.repeat_for(play) == "track"


def test_a_new_play_that_does_not_say_takes_the_setting() -> None:
    preferences = chose(shuffle_on_play=True, repeat_mode="context")

    assert preferences.shuffle_for(PlayRequest(context_uri=ALBUM)) is True
    assert preferences.repeat_for(PlayRequest(context_uri=ALBUM)) == "context"


def test_a_resume_leaves_both_alone_whatever_the_setting() -> None:
    preferences = chose(shuffle_on_play=True, repeat_mode="track")

    assert preferences.shuffle_for(PlayRequest()) is None
    assert preferences.repeat_for(PlayRequest()) is None


def test_a_setting_left_off_never_turns_a_mode_off() -> None:
    untouched = chose()

    assert untouched.shuffle_for(PlayRequest(uris=[TRACK])) is None
    assert untouched.repeat_for(PlayRequest(uris=[TRACK])) is None


def test_all_of_holds_only_when_every_part_does() -> None:
    both = all_of(is_playing(), shuffle_is(shuffle=True))

    assert both(player_state(shuffle_state=True))
    assert not both(player_state(shuffle_state=False))
    assert not both(player_state(is_playing=False, shuffle_state=True))
    assert all_of()(player_state())


# -- over HTTP ------------------------------------------------------------------------------


async def test_a_play_with_nothing_chosen_sends_one_command(
    client: httpx.AsyncClient, spotify: FakeSpotify
) -> None:
    spotify.on_command = PLAYING

    response = await client.post("/v1/player/play", json={"uris": [TRACK]})

    assert response.status_code == 200
    assert [path for path, _params in sent(spotify)] == ["/me/player/play"]


async def test_the_persons_settings_are_applied_to_a_new_play_and_confirmed(
    client: httpx.AsyncClient, spotify: FakeSpotify, chosen: FakeSettingsClient
) -> None:
    chosen.seed("spotify", {"shuffle_on_play": True, "repeat_mode": "context"})
    spotify.on_command = {**PLAYING, "shuffle_state": True, "repeat_state": "context"}

    response = await client.post(
        "/v1/player/play", json={"context_uri": ALBUM, "device_id": "device-1"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["shuffle_state"] is True
    assert body["repeat_state"] == "context"
    assert sent(spotify) == [
        ("/me/player/play", {"device_id": "device-1"}),
        ("/me/player/shuffle", {"state": "true", "device_id": "device-1"}),
        ("/me/player/repeat", {"state": "context", "device_id": "device-1"}),
    ]


async def test_a_resume_is_one_command_whatever_was_chosen(
    client: httpx.AsyncClient, spotify: FakeSpotify, chosen: FakeSettingsClient
) -> None:
    chosen.seed("spotify", {"shuffle_on_play": True, "repeat_mode": "track"})
    spotify.on_command = PLAYING

    response = await client.post("/v1/player/play", json={})

    assert response.status_code == 200
    assert [path for path, _params in sent(spotify)] == ["/me/player/play"]


async def test_a_request_can_ask_for_shuffle_alone(
    client: httpx.AsyncClient, spotify: FakeSpotify
) -> None:
    spotify.on_command = {**PLAYING, "shuffle_state": True}

    response = await client.post("/v1/player/play", json={"uris": [TRACK], "shuffle": True})

    assert response.status_code == 200
    assert [path for path, _params in sent(spotify)] == ["/me/player/play", "/me/player/shuffle"]


async def test_a_request_can_ask_for_repeat_alone_and_turn_it_off(
    client: httpx.AsyncClient, spotify: FakeSpotify, chosen: FakeSettingsClient
) -> None:
    chosen.seed("spotify", {"repeat_mode": "track"})
    spotify.state = player_state(repeat_state="track")
    spotify.on_command = {**PLAYING, "repeat_state": "off"}

    response = await client.post("/v1/player/play", json={"uris": [TRACK], "repeat": "off"})

    assert response.status_code == 200
    assert response.json()["repeat_state"] == "off"
    assert sent(spotify)[1] == ("/me/player/repeat", {"state": "off"})


async def test_a_mode_that_does_not_take_is_a_confirmation_timeout(
    client: httpx.AsyncClient, spotify: FakeSpotify
) -> None:
    # Playback starts, and the device never reports the shuffle it was asked for.
    spotify.on_command = PLAYING

    response = await client.post("/v1/player/play", json={"uris": [TRACK], "shuffle": True})

    assert response.status_code == 504
    problem = response.json()
    assert problem["type"] == problem_type("confirmation-timeout")
    assert problem["details"]["observed"]["shuffle_state"] is False


async def test_a_value_settings_api_should_never_send_is_treated_as_off(
    client: httpx.AsyncClient, spotify: FakeSpotify, chosen: FakeSettingsClient
) -> None:
    chosen.seed("spotify", {"shuffle_on_play": "yes", "repeat_mode": "forever"})
    spotify.on_command = PLAYING

    response = await client.post("/v1/player/play", json={"uris": [TRACK]})

    assert response.status_code == 200
    assert [path for path, _params in sent(spotify)] == ["/me/player/play"]
