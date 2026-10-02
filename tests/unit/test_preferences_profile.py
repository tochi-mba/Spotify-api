"""A person's choices are read for the profile the request runs as.

The bug, named: spotify-api asked settings-api for the ``spotify`` namespace without naming a
profile. Every ``spotify`` setting is profile-scoped, and settings-api returns a profile's
values only to a resolve that names it, so every choice a person made reached this service
as the catalogue default: market, batch size, confirm timeout, shuffle, repeat, and
``allow_explicit``, which meant explicit tracks were played to someone who had turned them
off. The shared test fake ignored the profile too, so no test noticed.
"""

from __future__ import annotations

from settings_client import Fallback, OnUnavailable
from settings_client.testing import FakeSettingsClient

from spotify_api.preferences import SettingsApiPreferences
from tests.factories import make_settings

TOKEN = "a-user-token-from-keyring"


def reading(client: FakeSettingsClient) -> SettingsApiPreferences:
    return SettingsApiPreferences(
        client=client, settings=make_settings(keyring_default_profile="personal")
    )


def chosen_in(profile: str) -> FakeSettingsClient:
    client = FakeSettingsClient()
    client.seed(
        "spotify",
        {"allow_explicit": False, "shuffle_on_play": True, "default_market": "PT"},
        profile=profile,
    )
    return client


async def test_a_named_profile_is_the_one_asked_for_and_its_choices_apply() -> None:
    client = chosen_in("family")

    preferences = await reading(client).for_token(TOKEN, profile="family")

    assert client.asked == [("spotify", "family")]
    assert preferences.allow_explicit is False
    assert preferences.shuffle_on_play is True
    assert preferences.default_market == "PT"


async def test_with_no_profile_named_the_persons_default_profile_is_asked_for() -> None:
    client = chosen_in("family")
    client.seed("spotify", {"default_profile": "family"})

    preferences = await reading(client).for_token(TOKEN)

    assert client.asked == [("spotify", None), ("spotify", "family")]
    assert preferences.allow_explicit is False
    assert preferences.profile(None) == "family"


async def test_with_no_default_chosen_the_deployments_default_profile_is_asked_for() -> None:
    client = chosen_in("personal")

    preferences = await reading(client).for_token(TOKEN)

    assert client.asked == [("spotify", None), ("spotify", "personal")]
    assert preferences.allow_explicit is False


async def test_another_profiles_choices_do_not_apply() -> None:
    client = chosen_in("family")

    preferences = await reading(client).for_token(TOKEN, profile="work")

    assert preferences.allow_explicit is True
    assert preferences.shuffle_on_play is False


async def test_a_default_profile_that_cannot_be_read_asks_nothing_more() -> None:
    client = FakeSettingsClient()
    client.unavailable = True
    client.seed_fallback(
        "spotify",
        "default_profile",
        Fallback(default="personal", on_unavailable=OnUnavailable.REFUSE),
    )

    preferences = await reading(client).for_token(TOKEN)

    assert client.asked == [("spotify", None)]
    assert preferences.default_profile is None
