"""One person's choices, and what this service does with them -- and without them.

Every test that reads settings-api here uses its shared fake, including with it switched
off, because the outage is the case most services forget and the one their users notice.
"""

from __future__ import annotations

from typing import Any

import pytest
from settings_client import Fallback, OnUnavailable
from settings_client.testing import FakeSettingsClient

from spotify_api.errors import PreferencesUnavailableError
from spotify_api.logging import configure_logging
from spotify_api.preferences import (
    NOT_GUESSED,
    PROFILE_UNKNOWN,
    REFUSED,
    DeploymentPreferences,
    Preferences,
    SettingsApiPreferences,
    build_preference_source,
    deployment_preferences,
)
from tests.conftest import log_records
from tests.factories import make_settings

USER_TOKEN = "a-user-token-from-keyring"
SETTINGS_API_TOKEN = "settings-api-token-for-spotify-api-01"

FALLBACKS = {
    "default_market": Fallback(default=None, on_unavailable=OnUnavailable.USE_DEFAULT),
    "max_batch_size": Fallback(default=50, on_unavailable=OnUnavailable.USE_DEFAULT),
    "confirm_timeout_seconds": Fallback(default=15, on_unavailable=OnUnavailable.USE_DEFAULT),
    "default_profile": Fallback(default="personal", on_unavailable=OnUnavailable.REFUSE),
}


@pytest.fixture(autouse=True)
def _logs() -> None:
    configure_logging(level="INFO", log_format="json")


def reading(client: FakeSettingsClient, **overrides: Any) -> SettingsApiPreferences:
    return SettingsApiPreferences(client=client, settings=make_settings(**overrides))


class TestWithoutSettingsApi:
    def test_the_configuration_is_what_everybody_gets(self) -> None:
        settings = make_settings(
            default_market="GB",
            max_batch_size=10,
            confirm_timeout_seconds=20.0,
            keyring_default_profile="work",
        )

        assert deployment_preferences(settings) == Preferences(
            default_market="GB",
            max_batch_size=10,
            confirm_timeout_seconds=20.0,
            default_profile="work",
        )

    async def test_nobody_is_asked_when_settings_api_is_not_configured(self) -> None:
        settings = make_settings()
        source = build_preference_source(settings)

        assert isinstance(source, DeploymentPreferences)
        assert await source.for_token(USER_TOKEN) == deployment_preferences(settings)
        await source.aclose()

    async def test_a_configured_settings_api_is_asked_per_person(self) -> None:
        source = build_preference_source(
            make_settings(
                settings_api_base_url="https://settings.test",
                settings_api_token=SETTINGS_API_TOKEN,
            )
        )

        assert isinstance(source, SettingsApiPreferences)
        await source.aclose()

    async def test_a_substituted_client_is_the_one_asked(self) -> None:
        client = FakeSettingsClient()

        await build_preference_source(make_settings(), client=client).for_token(USER_TOKEN)

        assert client.resolves == 1


class TestAPersonsChoices:
    async def test_they_become_the_limits_a_request_runs_under(self) -> None:
        client = FakeSettingsClient()
        client.seed(
            "spotify",
            {
                "default_market": "PT",
                "max_batch_size": 8,
                "confirm_timeout_seconds": 30,
                "default_profile": "work",
            },
        )

        preferences = await reading(client, confirm_timeout_seconds=60.0).for_token(USER_TOKEN)

        assert preferences == Preferences(
            default_market="PT",
            max_batch_size=8,
            confirm_timeout_seconds=30.0,
            default_profile="work",
        )

    async def test_a_ceiling_can_be_narrowed_and_never_raised(self) -> None:
        client = FakeSettingsClient()
        client.seed("spotify", {"max_batch_size": 80, "confirm_timeout_seconds": 200})

        preferences = await reading(
            client, max_batch_size=10, confirm_timeout_seconds=15.0
        ).for_token(USER_TOKEN)

        assert preferences.max_batch_size == 10
        assert preferences.confirm_timeout_seconds == 15.0

    async def test_an_explicit_null_market_is_kept(self) -> None:
        client = FakeSettingsClient()
        client.seed("spotify", {"default_market": None})

        preferences = await reading(client, default_market="US").for_token(USER_TOKEN)

        assert preferences.default_market is None

    async def test_a_lowercase_market_is_normalised(self) -> None:
        client = FakeSettingsClient()
        client.seed("spotify", {"default_market": "gb"})

        preferences = await reading(client).for_token(USER_TOKEN)

        assert preferences.default_market == "GB"

    async def test_a_request_with_no_caller_asks_nobody(self) -> None:
        client = FakeSettingsClient()
        settings = make_settings()

        preferences = await SettingsApiPreferences(client=client, settings=settings).for_token(None)

        assert preferences == deployment_preferences(settings)
        assert client.resolves == 0


class TestWhenSettingsApiCannotBeReached:
    async def test_never_having_answered_leaves_the_configuration_and_unknown_profile(
        self,
    ) -> None:
        client = FakeSettingsClient()
        client.unavailable = True
        settings = make_settings(default_market="US", max_batch_size=7)

        preferences = await SettingsApiPreferences(client=client, settings=settings).for_token(
            USER_TOKEN
        )

        assert preferences.default_market == "US"
        assert preferences.max_batch_size == 7
        assert preferences.confirm_timeout_seconds == settings.confirm_timeout_seconds
        assert preferences.default_profile is None

    async def test_known_fallbacks_are_used_and_the_profile_is_still_not_guessed(self) -> None:
        client = FakeSettingsClient(fallbacks={"spotify": FALLBACKS})
        client.unavailable = True

        preferences = await reading(client, max_batch_size=10, default_market="US").for_token(
            USER_TOKEN
        )

        assert preferences.default_market is None
        assert preferences.max_batch_size == 10
        assert preferences.confirm_timeout_seconds == 15.0
        assert preferences.default_profile is None

    async def test_a_spotify_setting_that_refuses_fails_rather_than_being_guessed(self) -> None:
        refusing = {"max_batch_size": Fallback(default=50, on_unavailable=OnUnavailable.REFUSE)}
        client = FakeSettingsClient(fallbacks={"spotify": refusing})
        client.unavailable = True

        with pytest.raises(PreferencesUnavailableError, match=NOT_GUESSED):
            await reading(client).for_token(USER_TOKEN)


class TestWhenSettingsApiRefusesThisService:
    async def test_the_refusal_is_not_hidden_behind_defaults(self) -> None:
        client = FakeSettingsClient()
        client.rejects["spotify"] = (403, "spotify-api was not granted spotify")

        with pytest.raises(PreferencesUnavailableError) as caught:
            await reading(client).for_token(USER_TOKEN)

        assert str(caught.value) == REFUSED
        assert "granted" not in str(caught.value)

    async def test_the_refusal_logs_the_status_and_never_the_detail(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        client = FakeSettingsClient()
        client.rejects["spotify"] = (403, "spotify-api was not granted spotify")

        with pytest.raises(PreferencesUnavailableError):
            await reading(client).for_token(USER_TOKEN)

        output = capsys.readouterr().out
        [record] = [r for r in log_records(output) if r["event"] == "settings_rejected"]
        assert record["status_code"] == 403
        assert "granted" not in output
        assert USER_TOKEN not in output


class TestValuesThatCannotBeUsed:
    @pytest.mark.parametrize("value", [True, "8", 0, -1, None])
    async def test_an_unusable_batch_cap_leaves_the_configuration(self, value: Any) -> None:
        client = FakeSettingsClient()
        client.seed("spotify", {"max_batch_size": value})

        preferences = await reading(client, max_batch_size=9).for_token(USER_TOKEN)

        assert preferences.max_batch_size == 9

    @pytest.mark.parametrize("value", [True, "15", 0, -1, 1.5, None])
    async def test_an_unusable_timeout_leaves_the_configuration(self, value: Any) -> None:
        client = FakeSettingsClient()
        client.seed("spotify", {"confirm_timeout_seconds": value})

        preferences = await reading(client, confirm_timeout_seconds=12.0).for_token(USER_TOKEN)

        assert preferences.confirm_timeout_seconds == 12.0

    @pytest.mark.parametrize("value", [True, 12, "GBR", "g"])
    async def test_an_unusable_market_leaves_the_configuration(self, value: Any) -> None:
        client = FakeSettingsClient()
        client.seed("spotify", {"default_market": value})

        preferences = await reading(client, default_market="US").for_token(USER_TOKEN)

        assert preferences.default_market == "US"

    async def test_a_blank_market_is_null(self) -> None:
        client = FakeSettingsClient()
        client.seed("spotify", {"default_market": "  "})

        preferences = await reading(client, default_market="US").for_token(USER_TOKEN)

        assert preferences.default_market is None

    async def test_a_missing_setting_leaves_the_configuration(self) -> None:
        client = FakeSettingsClient()
        client.seed("spotify", {})
        settings = make_settings(default_market="US", max_batch_size=9)

        preferences = await SettingsApiPreferences(client=client, settings=settings).for_token(
            USER_TOKEN
        )

        assert preferences.default_market == "US"
        assert preferences.max_batch_size == 9
        assert preferences.confirm_timeout_seconds == settings.confirm_timeout_seconds
        assert preferences.default_profile == settings.keyring_default_profile

    async def test_the_key_is_logged_and_the_value_never_is(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        client = FakeSettingsClient()
        client.seed("spotify", {"max_batch_size": "a-value-nobody-should-read"})

        await reading(client).for_token(USER_TOKEN)

        output = capsys.readouterr().out
        [record] = [r for r in log_records(output) if r["event"] == "setting_unusable"]
        assert record["key"] == "max_batch_size"
        assert "a-value-nobody-should-read" not in output
        assert USER_TOKEN not in output

    @pytest.mark.parametrize("value", [7, ""])
    async def test_a_profile_that_is_not_a_name_leaves_the_configuration(self, value: Any) -> None:
        client = FakeSettingsClient()
        client.seed("spotify", {"default_profile": value})

        preferences = await reading(client, keyring_default_profile="default").for_token(USER_TOKEN)

        assert preferences.default_profile == "default"


class TestChoosingAProfile:
    def test_a_named_profile_is_used_as_named(self) -> None:
        preferences = deployment_preferences(make_settings())

        assert preferences.profile("work") == "work"

    def test_the_default_fills_in_when_none_is_named(self) -> None:
        preferences = deployment_preferences(make_settings(keyring_default_profile="personal"))

        assert preferences.profile(None) == "personal"

    def test_an_unknown_default_refuses_rather_than_guessing(self) -> None:
        preferences = Preferences(
            default_market=None,
            max_batch_size=1,
            confirm_timeout_seconds=1.0,
            default_profile=None,
        )

        with pytest.raises(PreferencesUnavailableError) as caught:
            preferences.profile(None)

        assert str(caught.value) == PROFILE_UNKNOWN

    def test_a_named_profile_needs_no_default(self) -> None:
        preferences = Preferences(
            default_market=None,
            max_batch_size=1,
            confirm_timeout_seconds=1.0,
            default_profile=None,
        )

        assert preferences.profile("work") == "work"


class TestStaleReads:
    async def test_a_stale_document_is_logged(self, capsys: pytest.CaptureFixture[str]) -> None:
        client = FakeSettingsClient(fallbacks={"spotify": FALLBACKS})
        client.unavailable = True

        await reading(client).for_token(USER_TOKEN)

        events = [r["event"] for r in log_records(capsys.readouterr().out)]
        assert "settings_stale" in events
