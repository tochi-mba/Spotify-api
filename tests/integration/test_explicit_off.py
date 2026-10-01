"""A person who turned explicit tracks off is offered none and played none.

The bug, named: `spotify.allow_explicit` could be set and read back, and changed nothing.
A lookup returned whatever Spotify ranked first, and a play played it, whether or not the
person had turned explicit tracks off.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
import pytest
from settings_client import Fallback, OnUnavailable
from settings_client.testing import FakeSettingsClient

from spotify_api.app import create_app
from spotify_api.preferences import (
    EXPLICIT_UNKNOWN,
    Preferences,
    SettingsApiPreferences,
    build_preference_source,
)
from spotify_api.spotify.mappers import first_clean_track
from tests.factories import make_settings, problem_type
from tests.integration.test_player_routes import SPOTIFY_URL, USER_TOKEN, FakeSpotify

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

CLEAN = "spotify:track:clean00000000000000000"
EXPLICIT = "spotify:track:explicit0000000000000"
EPISODE = "spotify:episode:talk0000000000000000"
PLAYING = {"is_playing": True, "item": {"uri": CLEAN}}

FALLBACKS = {
    "default_market": Fallback(default=None, on_unavailable=OnUnavailable.USE_DEFAULT),
    "allow_explicit": Fallback(default=True, on_unavailable=OnUnavailable.REFUSE),
    "default_profile": Fallback(default="personal", on_unavailable=OnUnavailable.USE_DEFAULT),
}


def track(uri: str, *, explicit: bool, name: str = "Song") -> dict[str, Any]:
    return {
        "id": uri.rsplit(":", maxsplit=1)[-1],
        "uri": uri,
        "name": name,
        "explicit": explicit,
        "duration_ms": 200_000,
        "artists": [{"id": "a1", "name": "Somebody", "uri": "spotify:artist:a1"}],
        "album": {"id": "al1", "name": "Record", "uri": "spotify:album:al1"},
    }


class Catalogue(FakeSpotify):
    """The fake player, with a search and a track lookup in front of it."""

    def __init__(self) -> None:
        super().__init__()
        self.matches: list[dict[str, Any]] = []
        self.explicit: set[str] = {EXPLICIT}
        self.reads: list[tuple[str, dict[str, str]]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/v1")
        if request.url.host != "keyring.test" and request.method == "GET":
            if path == "/search":
                self.reads.append((path, dict(request.url.params)))
                limit = int(request.url.params["limit"])
                return httpx.Response(200, json={"tracks": {"items": self.matches[:limit]}})
            if path == "/tracks":
                self.reads.append((path, dict(request.url.params)))
                ids = request.url.params["ids"].split(",")
                tracks = [
                    track(f"spotify:track:{i}", explicit=f"spotify:track:{i}" in self.explicit)
                    for i in ids
                ]
                return httpx.Response(200, json={"tracks": [*tracks, None]})
        return super().__call__(request)


@pytest.fixture
def spotify() -> Catalogue:
    return Catalogue()


@pytest.fixture
def chosen() -> FakeSettingsClient:
    return FakeSettingsClient(fallbacks={"spotify": FALLBACKS})


@pytest.fixture
async def client(
    spotify: Catalogue, chosen: FakeSettingsClient
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
            headers={"Authorization": f"Bearer {USER_TOKEN}", "X-Keyring-Profile": "personal"},
        ) as http_client:
            yield http_client


def commands(spotify: Catalogue) -> list[str]:
    return [path for _method, path, _params in spotify.commands]


def off(chosen: FakeSettingsClient) -> None:
    chosen.seed("spotify", {"allow_explicit": False})


# -- which match is the clean one -----------------------------------------------------------


def test_the_first_match_not_marked_explicit_is_the_one_offered() -> None:
    first = track(EXPLICIT, explicit=True)
    second = track(CLEAN, explicit=False)

    assert first_clean_track({"tracks": {"items": [first, second]}}) == (second, False)
    assert first_clean_track({"tracks": {"items": [first]}}) == (None, True)
    assert first_clean_track({"tracks": {"items": []}}) == (None, False)
    assert first_clean_track({}) == (None, False)


# -- reading the choice ---------------------------------------------------------------------


async def read(chosen: FakeSettingsClient) -> Preferences:
    return await SettingsApiPreferences(client=chosen, settings=make_settings()).for_token("t")


async def test_a_persons_answer_is_read(chosen: FakeSettingsClient) -> None:
    off(chosen)

    assert (await read(chosen)).explicit_allowed() is False


async def test_a_settings_api_with_no_such_key_allows_them_as_before() -> None:
    assert (await read(FakeSettingsClient())).explicit_allowed() is True


async def test_something_that_is_not_a_yes_or_a_no_is_not_known(
    chosen: FakeSettingsClient,
) -> None:
    chosen.seed("spotify", {"allow_explicit": "sometimes"})

    assert (await read(chosen)).allow_explicit is None


async def test_an_outage_leaves_it_unknown_whether_or_not_defaults_were_ever_seen(
    chosen: FakeSettingsClient,
) -> None:
    await read(chosen)
    chosen.unavailable = True
    stale = await read(chosen)
    never_answered = FakeSettingsClient()
    never_answered.unavailable = True

    assert stale.allow_explicit is None
    assert (await read(never_answered)).allow_explicit is None


# -- a lookup -------------------------------------------------------------------------------


async def lookup(client: httpx.AsyncClient) -> dict[str, Any]:
    response = await client.post("/v1/lookup", json={"items": [{"name": "Song"}]})
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()["results"][0]
    return result


async def test_with_explicit_allowed_the_first_match_is_the_answer(
    client: httpx.AsyncClient, spotify: Catalogue
) -> None:
    spotify.matches = [track(EXPLICIT, explicit=True), track(CLEAN, explicit=False)]

    result = await lookup(client)

    assert result["track"]["uri"] == EXPLICIT
    assert result["withheld"] is None
    assert spotify.reads[0][1]["limit"] == "1"


async def test_with_explicit_off_the_clean_version_is_offered(
    client: httpx.AsyncClient, spotify: Catalogue, chosen: FakeSettingsClient
) -> None:
    off(chosen)
    spotify.matches = [track(EXPLICIT, explicit=True), track(CLEAN, explicit=False)]

    result = await lookup(client)

    assert result["status"] == "found"
    assert result["track"]["uri"] == CLEAN
    assert result["track"]["explicit"] is False
    assert spotify.reads[0][1]["limit"] == "10"


async def test_when_every_match_is_explicit_nothing_is_offered_and_it_says_why(
    client: httpx.AsyncClient, spotify: Catalogue, chosen: FakeSettingsClient
) -> None:
    off(chosen)
    spotify.matches = [track(EXPLICIT, explicit=True)]

    result = await lookup(client)

    assert result["status"] == "not_found"
    assert result["track"] is None
    assert result["withheld"] == "explicit"


async def test_nothing_matching_is_plain_not_found(
    client: httpx.AsyncClient, chosen: FakeSettingsClient
) -> None:
    off(chosen)

    result = await lookup(client)

    assert result["status"] == "not_found"
    assert result["withheld"] is None


# -- a play and a queue ---------------------------------------------------------------------


async def test_an_explicit_track_is_refused_before_anything_reaches_the_player(
    client: httpx.AsyncClient, spotify: Catalogue, chosen: FakeSettingsClient
) -> None:
    off(chosen)

    response = await client.post("/v1/player/play", json={"uris": [CLEAN, EXPLICIT]})

    assert response.status_code == 403
    problem = response.json()
    assert problem["type"] == problem_type("explicit-not-allowed")
    assert problem["detail"] == (
        "1 of the tracks named is marked explicit, and your settings leave those out; "
        "nothing was played"
    )
    assert problem["details"]["explicit"] == [EXPLICIT]
    assert commands(spotify) == []


async def test_several_explicit_tracks_are_all_named(
    client: httpx.AsyncClient, spotify: Catalogue, chosen: FakeSettingsClient
) -> None:
    off(chosen)
    other = "spotify:track:explicit1111111111111"
    spotify.explicit.add(other)

    response = await client.post("/v1/player/play", json={"uris": [other, CLEAN, EXPLICIT]})

    assert response.json()["detail"].startswith("2 of the tracks named are marked explicit")
    assert response.json()["details"]["explicit"] == [other, EXPLICIT]


async def test_clean_tracks_play_and_an_episode_is_not_asked_about(
    client: httpx.AsyncClient, spotify: Catalogue, chosen: FakeSettingsClient
) -> None:
    off(chosen)
    spotify.on_command = PLAYING

    response = await client.post("/v1/player/play", json={"uris": [CLEAN, EPISODE]})

    assert response.status_code == 200
    assert commands(spotify) == ["/me/player/play"]
    assert spotify.reads == [("/tracks", {"ids": "clean00000000000000000"})]


async def test_more_tracks_than_one_call_holds_are_asked_about_in_several(
    client: httpx.AsyncClient, spotify: Catalogue, chosen: FakeSettingsClient
) -> None:
    off(chosen)
    many = [f"spotify:track:{number:022d}" for number in range(120)]
    spotify.on_command = {"is_playing": True, "item": {"uri": many[0]}}

    response = await client.post("/v1/player/play", json={"uris": many})

    assert response.status_code == 200
    assert [len(params["ids"].split(",")) for _path, params in spotify.reads] == [50, 50, 20]


async def test_with_explicit_allowed_a_play_asks_spotify_nothing_first(
    client: httpx.AsyncClient, spotify: Catalogue
) -> None:
    spotify.on_command = {"is_playing": True, "item": {"uri": EXPLICIT}}

    response = await client.post("/v1/player/play", json={"uris": [EXPLICIT]})

    assert response.status_code == 200
    assert spotify.reads == []


async def test_an_album_or_a_resume_is_not_looked_inside(
    client: httpx.AsyncClient, spotify: Catalogue, chosen: FakeSettingsClient
) -> None:
    off(chosen)
    spotify.on_command = {"is_playing": True}

    album = await client.post("/v1/player/play", json={"context_uri": "spotify:album:abc"})
    resume = await client.post("/v1/player/play", json={})

    assert (album.status_code, resume.status_code) == (200, 200)
    assert spotify.reads == []


async def test_an_explicit_track_is_not_queued(
    client: httpx.AsyncClient, spotify: Catalogue, chosen: FakeSettingsClient
) -> None:
    off(chosen)

    refused = await client.post("/v1/player/queue", json={"uri": EXPLICIT})
    queued = await client.post("/v1/player/queue", json={"uri": CLEAN})

    assert refused.status_code == 403
    assert refused.json()["type"] == problem_type("explicit-not-allowed")
    assert queued.status_code == 200
    assert commands(spotify) == ["/me/player/queue"]


# -- when the answer cannot be read ---------------------------------------------------------


async def test_an_outage_refuses_what_needs_the_answer_and_nothing_else(
    client: httpx.AsyncClient, spotify: Catalogue, chosen: FakeSettingsClient
) -> None:
    chosen.unavailable = True
    spotify.on_command = {"is_playing": False}

    found = await client.post("/v1/lookup", json={"items": [{"name": "Song"}]})
    named = await client.post("/v1/player/play", json={"uris": [CLEAN]})
    queued = await client.post("/v1/player/queue", json={"uri": CLEAN})
    paused = await client.post("/v1/player/pause")
    spotify.on_command = {"is_playing": True}
    resumed = await client.post("/v1/player/play", json={})

    for refused in (found, named, queued):
        assert refused.status_code == 503
        assert refused.json()["type"] == problem_type("preferences-unavailable")
        assert refused.json()["detail"] == EXPLICIT_UNKNOWN
    assert (paused.status_code, resumed.status_code) == (200, 200)
    assert commands(spotify) == ["/me/player/pause", "/me/player/play"]
