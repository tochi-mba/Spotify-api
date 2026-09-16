"""Playback control: issue the command, then prove it took effect."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from keyring_client import JWKS_PATH
from keyring_client.testing import jwks

from spotify_api.app import create_app
from tests.factories import make_settings, problem_type, user_token

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

TRACK = "spotify:track:7tFiyTwD0nx5a1eklYtX2J"
OTHER = "spotify:track:0000000000000000000000"
USER_TOKEN = user_token("player-account")
SPOTIFY_URL = "https://api.spotify.test/v1"


def player_state(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "is_playing": True,
        "progress_ms": 0,
        "shuffle_state": False,
        "repeat_state": "off",
        "device": {
            "id": "device-1",
            "name": "Kitchen",
            "type": "Speaker",
            "is_active": True,
            "volume_percent": 50,
            "supports_volume": True,
        },
        "item": {"uri": TRACK, "name": "Bohemian Rhapsody"},
        "currently_playing_type": "track",
    }
    base.update(overrides)
    return base


class FakeSpotify:
    """keyring plus Spotify, with a mutable player the commands actually move."""

    def __init__(self) -> None:
        self.state: dict[str, Any] | None = player_state()
        self.commands: list[tuple[str, str, dict[str, str]]] = []
        self.bodies: list[Any] = []
        self.command_status = 204
        self.command_error: dict[str, Any] | None = None
        #: Applied to self.state when a command is accepted, so the confirmer
        #: has something true to observe.
        self.on_command: dict[str, Any] | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "keyring.test":
            return self._keyring(request)

        path = request.url.path.removeprefix("/v1")
        if request.method == "GET" and path == "/me/player":
            if self.state is None:
                return httpx.Response(204)
            return httpx.Response(200, json=self.state)
        if request.method == "GET":
            return httpx.Response(200, json=self._read(path))

        self.commands.append((request.method, path, dict(request.url.params)))
        self.bodies.append(request.read() or None)
        if self.command_status != 204:
            return httpx.Response(
                self.command_status, json=self.command_error or {"error": {"message": "no"}}
            )
        if self.on_command is not None and self.state is not None:
            self.state = {**self.state, **self.on_command}
        return httpx.Response(204)

    @staticmethod
    def _keyring(request: httpx.Request) -> httpx.Response:
        """Keyring's published keys, or the Spotify credential it would hand over."""
        if request.url.path == JWKS_PATH:
            return httpx.Response(200, json=jwks())
        return httpx.Response(
            200,
            json={
                "service": "spotify",
                "headers": {"Authorization": "Bearer spotify-token"},
                "query_params": {},
                "expires_at": None,
            },
        )

    @staticmethod
    def _read(path: str) -> dict[str, Any]:
        if path == "/me/player/devices":
            return {"devices": [{"id": "device-1", "name": "Kitchen", "type": "Speaker"}]}
        if path == "/me/player/queue":
            return {"currently_playing": {"uri": TRACK}, "queue": [{"uri": OTHER}]}
        if path == "/me/player/recently-played":
            return {"items": [{"track": {"uri": TRACK}, "played_at": "2026-09-10T12:00:00Z"}]}
        return player_state()


@pytest.fixture
def spotify() -> FakeSpotify:
    return FakeSpotify()


@pytest.fixture
async def client(spotify: FakeSpotify) -> AsyncIterator[httpx.AsyncClient]:
    settings = make_settings(
        keyring_base_url="https://keyring.test",
        spotify_base_url=SPOTIFY_URL,
        confirm_poll_interval_seconds=0.001,
        confirm_timeout_seconds=0.05,
    )
    app = create_app(settings=settings, transport=httpx.MockTransport(spotify))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"Authorization": f"Bearer {USER_TOKEN}"},
        ) as http_client:
            yield http_client


# -- reads ------------------------------------------------------------------


async def test_playback_state_is_returned(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/player")

    assert response.status_code == 200
    body = response.json()
    assert body["is_playing"] is True
    assert body["device"]["name"] == "Kitchen"
    assert body["item"]["uri"] == TRACK


async def test_nothing_playing_answers_null(
    client: httpx.AsyncClient, spotify: FakeSpotify
) -> None:
    # Spotify answers 204 when no device is active; that is not an error.
    spotify.state = None
    response = await client.get("/v1/player")

    assert response.status_code == 200
    assert response.json() is None


async def test_devices_are_listed(client: httpx.AsyncClient) -> None:
    body = (await client.get("/v1/player/devices")).json()
    assert [d["name"] for d in body["devices"]] == ["Kitchen"]


async def test_the_queue_is_returned(client: httpx.AsyncClient) -> None:
    body = (await client.get("/v1/player/queue")).json()
    assert body["currently_playing"]["uri"] == TRACK
    assert body["queue"][0]["uri"] == OTHER


async def test_recently_played_is_returned(client: httpx.AsyncClient) -> None:
    body = (await client.get("/v1/player/recently-played?limit=5")).json()
    assert body["items"][0]["track"]["uri"] == TRACK


async def test_currently_playing_is_returned(client: httpx.AsyncClient) -> None:
    body = (await client.get("/v1/player/currently-playing")).json()
    assert body["item"]["uri"] == TRACK


# -- play -------------------------------------------------------------------


async def test_play_issues_the_command_and_confirms_the_right_track(
    client: httpx.AsyncClient, spotify: FakeSpotify
) -> None:
    spotify.state = player_state(is_playing=False, item={"uri": OTHER})
    spotify.on_command = {"is_playing": True, "item": {"uri": TRACK}}

    response = await client.post("/v1/player/play", json={"uris": [TRACK]})

    assert response.status_code == 200
    body = response.json()
    assert body["is_playing"] is True
    assert body["item"]["uri"] == TRACK

    method, path, _ = spotify.commands[0]
    assert (method, path) == ("PUT", "/me/player/play")
    assert b'"uris":["spotify:track:7tFiyTwD0nx5a1eklYtX2J"]' in spotify.bodies[0]


async def test_play_is_not_confirmed_by_a_different_track_already_playing(
    client: httpx.AsyncClient, spotify: FakeSpotify
) -> None:
    # The whole point: a device already playing something else must not count
    # as success just because is_playing is true.
    spotify.state = player_state(is_playing=True, item={"uri": OTHER})

    response = await client.post("/v1/player/play", json={"uris": [TRACK]})

    assert response.status_code == 504
    problem = response.json()
    assert problem["type"] == problem_type("confirmation-timeout")
    assert problem["details"]["observed"]["item"]["uri"] == OTHER


async def test_a_resume_confirms_only_that_something_is_playing(
    client: httpx.AsyncClient, spotify: FakeSpotify
) -> None:
    spotify.state = player_state(is_playing=False)
    spotify.on_command = {"is_playing": True}

    response = await client.post("/v1/player/play", json={})
    assert response.status_code == 200
    assert spotify.bodies[0] is None


async def test_play_from_a_context_sends_the_context_uri(
    client: httpx.AsyncClient, spotify: FakeSpotify
) -> None:
    spotify.on_command = {"is_playing": True}
    await client.post(
        "/v1/player/play",
        json={"context_uri": "spotify:album:abc", "offset": {"position": 3}},
    )
    assert b'"context_uri":"spotify:album:abc"' in spotify.bodies[0]
    assert b'"offset":{"position":3}' in spotify.bodies[0]


async def test_naming_both_a_context_and_uris_is_rejected(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/player/play", json={"context_uri": "spotify:album:abc", "uris": [TRACK]}
    )
    assert response.status_code == 422
    assert response.json()["type"] == problem_type("validation-failed")


async def test_a_device_id_is_passed_through(
    client: httpx.AsyncClient, spotify: FakeSpotify
) -> None:
    spotify.on_command = {"is_playing": True}
    await client.post("/v1/player/play", json={"device_id": "device-9"})
    assert spotify.commands[0][2]["device_id"] == "device-9"


# -- the other commands -----------------------------------------------------


async def test_pause_is_confirmed_by_playback_stopping(
    client: httpx.AsyncClient, spotify: FakeSpotify
) -> None:
    spotify.on_command = {"is_playing": False}
    response = await client.post("/v1/player/pause")

    assert response.status_code == 200
    assert response.json()["is_playing"] is False
    assert spotify.commands[0][:2] == ("PUT", "/me/player/pause")


async def test_next_is_confirmed_by_the_track_changing(
    client: httpx.AsyncClient, spotify: FakeSpotify
) -> None:
    spotify.on_command = {"item": {"uri": OTHER}}
    response = await client.post("/v1/player/next")

    assert response.status_code == 200
    assert response.json()["item"]["uri"] == OTHER
    assert spotify.commands[0][:2] == ("POST", "/me/player/next")


async def test_next_times_out_if_the_track_never_changes(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post("/v1/player/next")
    assert response.status_code == 504


async def test_previous_is_confirmed_by_the_track_changing(
    client: httpx.AsyncClient, spotify: FakeSpotify
) -> None:
    spotify.on_command = {"item": {"uri": OTHER}}
    response = await client.post("/v1/player/previous")

    assert response.status_code == 200
    assert spotify.commands[0][:2] == ("POST", "/me/player/previous")


async def test_seek_is_confirmed_by_the_position_moving(
    client: httpx.AsyncClient, spotify: FakeSpotify
) -> None:
    spotify.on_command = {"progress_ms": 60000}
    response = await client.post("/v1/player/seek", json={"position_ms": 60000})

    assert response.status_code == 200
    assert response.json()["progress_ms"] == 60000
    assert spotify.commands[0][2]["position_ms"] == "60000"


async def test_volume_is_confirmed_by_the_device_reporting_it(
    client: httpx.AsyncClient, spotify: FakeSpotify
) -> None:
    spotify.on_command = {"device": {"id": "device-1", "volume_percent": 80}}
    response = await client.post("/v1/player/volume", json={"volume_percent": 80})

    assert response.status_code == 200
    assert response.json()["device"]["volume_percent"] == 80


@pytest.mark.parametrize("volume", [-1, 101])
async def test_an_out_of_range_volume_is_rejected(client: httpx.AsyncClient, volume: int) -> None:
    response = await client.post("/v1/player/volume", json={"volume_percent": volume})
    assert response.status_code == 422


async def test_shuffle_is_confirmed_by_the_state_flipping(
    client: httpx.AsyncClient, spotify: FakeSpotify
) -> None:
    spotify.on_command = {"shuffle_state": True}
    response = await client.post("/v1/player/shuffle", json={"state": True})

    assert response.status_code == 200
    assert response.json()["shuffle_state"] is True
    assert spotify.commands[0][2]["state"] == "true"


async def test_repeat_is_confirmed_by_the_mode_changing(
    client: httpx.AsyncClient, spotify: FakeSpotify
) -> None:
    spotify.on_command = {"repeat_state": "track"}
    response = await client.post("/v1/player/repeat", json={"state": "track"})

    assert response.status_code == 200
    assert response.json()["repeat_state"] == "track"


async def test_an_unknown_repeat_mode_is_rejected(client: httpx.AsyncClient) -> None:
    response = await client.post("/v1/player/repeat", json={"state": "sometimes"})
    assert response.status_code == 422


async def test_transfer_is_confirmed_by_the_device_becoming_active(
    client: httpx.AsyncClient, spotify: FakeSpotify
) -> None:
    spotify.on_command = {"device": {"id": "device-9", "name": "Study"}}
    response = await client.post("/v1/player/transfer", json={"device_id": "device-9"})

    assert response.status_code == 200
    assert response.json()["device"]["id"] == "device-9"
    assert b'"device_ids":["device-9"]' in spotify.bodies[0]


async def test_queueing_does_not_require_playback_to_change(
    client: httpx.AsyncClient, spotify: FakeSpotify
) -> None:
    response = await client.post("/v1/player/queue", json={"uri": OTHER})

    assert response.status_code == 200
    assert spotify.commands[0][:2] == ("POST", "/me/player/queue")
    assert spotify.commands[0][2]["uri"] == OTHER


# -- failures ---------------------------------------------------------------


async def test_a_free_account_is_told_it_needs_premium(
    client: httpx.AsyncClient, spotify: FakeSpotify
) -> None:
    spotify.command_status = 403
    spotify.command_error = {"error": {"message": "Player command failed: Premium required"}}

    response = await client.post("/v1/player/play", json={"uris": [TRACK]})

    assert response.status_code == 403
    problem = response.json()
    assert problem["type"] == problem_type("premium-required")
    assert "Premium" in problem["detail"]


async def test_no_active_device_is_actionable(
    client: httpx.AsyncClient, spotify: FakeSpotify
) -> None:
    spotify.command_status = 404
    spotify.command_error = {"error": {"message": "No active device found"}}

    response = await client.post("/v1/player/play", json={"uris": [TRACK]})

    assert response.status_code == 409
    problem = response.json()
    assert problem["type"] == problem_type("no-active-device")
    assert "device_id" in problem["detail"]


async def test_a_command_accepted_with_nothing_playing_anywhere_says_so(
    client: httpx.AsyncClient, spotify: FakeSpotify
) -> None:
    # Spotify took the command, but no device ever reported anything.
    spotify.state = None
    response = await client.post("/v1/player/pause")

    assert response.status_code == 409
    assert response.json()["type"] == problem_type("no-active-device")


async def test_every_command_requires_a_user_token(spotify: FakeSpotify) -> None:
    settings = make_settings(keyring_base_url="https://keyring.test", spotify_base_url=SPOTIFY_URL)
    app = create_app(settings=settings, transport=httpx.MockTransport(spotify))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as anon:
            response = await anon.post("/v1/player/play", json={"uris": [TRACK]})
    assert response.status_code == 401


# -- async ------------------------------------------------------------------


async def test_a_command_can_be_backgrounded_and_polled(
    client: httpx.AsyncClient, spotify: FakeSpotify
) -> None:
    spotify.state = player_state(is_playing=False, item={"uri": OTHER})
    spotify.on_command = {"is_playing": True, "item": {"uri": TRACK}}

    accepted = await client.post("/v1/player/play?async=true", json={"uris": [TRACK]})
    assert accepted.status_code == 202
    body = accepted.json()
    assert body["operation"] == "player.play"

    for _ in range(50):
        await asyncio.sleep(0)
        job = (await client.get(body["poll_url"])).json()
        if job["status"] != "running":
            break

    assert job["status"] == "succeeded"
    assert job["result"]["item"]["uri"] == TRACK


async def test_a_backgrounded_command_records_its_failure(
    client: httpx.AsyncClient, spotify: FakeSpotify
) -> None:
    spotify.command_status = 403
    spotify.command_error = {"error": {"message": "Player command failed: Premium required"}}

    accepted = (await client.post("/v1/player/play?async=true", json={"uris": [TRACK]})).json()
    for _ in range(50):
        await asyncio.sleep(0)
        job = (await client.get(accepted["poll_url"])).json()
        if job["status"] not in ("pending", "running"):
            break

    assert job["status"] == "failed"
    assert job["error_type"] == "premium_required"


async def test_play_can_start_partway_into_a_track(
    client: httpx.AsyncClient, spotify: FakeSpotify
) -> None:
    spotify.state = player_state(is_playing=False)
    spotify.on_command = {"is_playing": True, "item": {"uri": TRACK}}

    response = await client.post("/v1/player/play", json={"uris": [TRACK], "position_ms": 45000})

    assert response.status_code == 200
    assert b'"position_ms":45000' in spotify.bodies[0]


async def test_transfer_can_start_playing_on_arrival(
    client: httpx.AsyncClient, spotify: FakeSpotify
) -> None:
    spotify.on_command = {"device": {"id": "device-9", "name": "Study"}}

    response = await client.post(
        "/v1/player/transfer", json={"device_id": "device-9", "play": True}
    )

    assert response.status_code == 200
    assert b'"play":true' in spotify.bodies[0]


async def test_skipping_with_nothing_playing_is_reported_as_no_device(
    client: httpx.AsyncClient, spotify: FakeSpotify
) -> None:
    # next reads the current track first to know what "changed" means. With
    # nothing loaded there is no baseline, and no device to skip on either.
    spotify.state = None

    response = await client.post("/v1/player/next")

    assert response.status_code == 409
    assert response.json()["type"] == problem_type("no-active-device")
