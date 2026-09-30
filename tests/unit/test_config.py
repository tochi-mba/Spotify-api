"""Settings load from SPOTIFY_API_* variables, validate hard, and never leak secrets."""

from __future__ import annotations

import os

import pytest
from pydantic import SecretStr, ValidationError

from spotify_api.config import (
    ENV_PREFIX,
    LEGACY_NAMES,
    Settings,
    UnknownSettingError,
    check_for_unknown_env_vars,
    get_settings,
    known_env_names,
)
from tests.factories import make_settings

ENV_SERVICE_TOKEN = "service-token-from-env-0123456789abcdef"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test from an environment holding none of this service's variables.

    Without this the suite depends on the machine it runs on: CI once exported
    ``ENVIRONMENT=test`` at workflow level and a defaults test went red there and nowhere
    else.
    """
    for name in list(os.environ):
        if name.startswith(ENV_PREFIX) or name in LEGACY_NAMES:
            monkeypatch.delenv(name, raising=False)


@pytest.fixture
def base_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPOTIFY_API_KEYRING_BASE_URL", "https://keyring.from-env")
    monkeypatch.setenv("SPOTIFY_API_KEYRING_SERVICE_TOKEN", ENV_SERVICE_TOKEN)


@pytest.mark.usefixtures("base_env")
def test_reads_keyring_configuration_from_the_environment() -> None:
    # Supplied by the environment, which mypy cannot see.
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.keyring_base_url == "https://keyring.from-env"
    assert settings.keyring_service_token.get_secret_value() == ENV_SERVICE_TOKEN


@pytest.mark.usefixtures("base_env")
def test_operational_defaults_are_sane() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.environment == "development"
    assert settings.log_level == "INFO"
    assert settings.log_format == "json"
    assert settings.spotify_base_url == "https://api.spotify.com/v1"
    assert settings.keyring_issuer == "http://127.0.0.1:8001"
    assert settings.keyring_audience == "spotify-api"
    assert settings.keyring_default_profile == "personal"
    assert settings.keyring_timeout_seconds == 5.0
    assert settings.jwks_cache_seconds == 3600.0
    assert settings.jwks_min_refetch_seconds == 60.0
    assert settings.credential_cache_skew_seconds == 60
    assert settings.credential_cache_default_ttl_seconds == 300
    assert settings.request_timeout_seconds == 10.0
    assert settings.max_retries == 3
    assert settings.retry_backoff_base_seconds == 0.2
    assert settings.max_concurrency == 8
    assert settings.max_batch_size == 50
    assert settings.default_market is None
    assert settings.settings_api is None


def test_missing_keyring_configuration_is_a_startup_failure() -> None:
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)  # type: ignore[call-arg]
    missing = {error["loc"][0] for error in excinfo.value.errors()}
    assert missing == {"keyring_base_url", "keyring_service_token"}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_retries", -1),
        ("max_concurrency", 0),
        ("max_batch_size", 0),
        ("request_timeout_seconds", 0.0),
        ("retry_backoff_base_seconds", -0.1),
        ("keyring_timeout_seconds", 0.0),
        ("credential_cache_skew_seconds", -1),
        ("jwks_cache_seconds", 0.0),
        ("jwks_min_refetch_seconds", 0.0),
    ],
)
def test_out_of_range_values_are_rejected(field: str, value: float) -> None:
    with pytest.raises(ValidationError):
        make_settings(**{field: value})


def test_trailing_slashes_are_stripped_from_base_urls() -> None:
    settings = make_settings(
        spotify_base_url="https://example.test/v1/",
        keyring_base_url="https://keyring.example.test//",
    )
    assert settings.spotify_base_url == "https://example.test/v1"
    assert settings.keyring_base_url == "https://keyring.example.test"


@pytest.mark.parametrize(("given", "expected"), [("gb", "GB"), ("  us  ", "US"), ("", None)])
def test_default_market_is_normalised(given: str, expected: str | None) -> None:
    assert make_settings(default_market=given).default_market == expected


def test_unknown_market_code_is_rejected() -> None:
    with pytest.raises(ValidationError):
        make_settings(default_market="GBR")


def test_a_short_service_token_is_refused_without_being_echoed() -> None:
    # keyring refuses any service token under 32 characters, so a shorter one here is a
    # deployment where every credential request fails; say so at startup instead.
    with pytest.raises(ValidationError) as excinfo:
        make_settings(keyring_service_token=SecretStr("too-short-token"))
    assert "too-short-token" not in str(excinfo.value)


@pytest.mark.parametrize("audience", ["", " spotify-api", "spotify-api.jobs"])
def test_an_audience_keyring_could_never_mint_for_this_service_is_refused(audience: str) -> None:
    with pytest.raises(ValidationError):
        make_settings(keyring_audience=audience)


@pytest.mark.usefixtures("base_env")
def test_the_service_token_never_appears_in_repr_or_dump() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    rendered = f"{settings!r} {settings.model_dump()} {settings.model_dump_json()}"
    assert ENV_SERVICE_TOKEN not in rendered
    assert "**********" in repr(settings)


def test_is_production_reflects_the_environment() -> None:
    assert make_settings(environment="production").is_production is True
    assert make_settings(environment="development").is_production is False


def test_an_invented_setting_is_refused_by_the_model_too() -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        make_settings(enable_everything=True)


# -- unknown and renamed variables --------------------------------------------


def test_every_field_has_a_prefixed_variable() -> None:
    assert "SPOTIFY_API_KEYRING_BASE_URL" in known_env_names()
    assert "SPOTIFY_API_SPOTIFY_BASE_URL" in known_env_names()
    assert all(name.startswith(ENV_PREFIX) for name in known_env_names())


def test_a_misspelled_setting_is_a_startup_error_not_a_silent_default() -> None:
    with pytest.raises(UnknownSettingError, match="SPOTIFY_API_DEFALT_MARKET"):
        check_for_unknown_env_vars({"SPOTIFY_API_DEFALT_MARKET": "GB"})


def test_a_name_from_before_the_prefix_says_what_replaced_it() -> None:
    # An upgrade that kept the old variables would otherwise start with every one of them
    # silently ignored -- including the market a person's searches resolve against.
    with pytest.raises(UnknownSettingError) as excinfo:
        check_for_unknown_env_vars(
            {"KEYRING_BASE_URL": "x", "DEFAULT_MARKET": "GB", "SPOTIFY_API_BASE_URL": "y"}
        )
    message = str(excinfo.value)
    assert "KEYRING_BASE_URL (now SPOTIFY_API_KEYRING_BASE_URL)" in message
    assert "DEFAULT_MARKET (now SPOTIFY_API_DEFAULT_MARKET)" in message
    assert "SPOTIFY_API_BASE_URL (now SPOTIFY_API_SPOTIFY_BASE_URL)" in message


def test_it_does_not_echo_the_values_it_refuses() -> None:
    with pytest.raises(UnknownSettingError) as excinfo:
        check_for_unknown_env_vars({"KEYRING_SERVICE_TOKEN": ENV_SERVICE_TOKEN})
    assert ENV_SERVICE_TOKEN not in str(excinfo.value)


def test_recognised_and_unrelated_variables_pass() -> None:
    # Generic names other software sets, and keyring's own variables on a shared host, are
    # none of this service's business.
    check_for_unknown_env_vars(
        {
            "SPOTIFY_API_KEYRING_BASE_URL": "x",
            "LOG_LEVEL": "DEBUG",
            "ENVIRONMENT": "test",
            "KEYRING_MASTER_KEY": "x",
        }
    )


@pytest.mark.usefixtures("base_env")
def test_get_settings_runs_the_check_before_building(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KEYRING_BASE_URL", "https://keyring.old-name")
    get_settings.cache_clear()
    try:
        with pytest.raises(UnknownSettingError):
            get_settings()
    finally:
        get_settings.cache_clear()


@pytest.mark.usefixtures("base_env")
def test_get_settings_is_cached() -> None:
    get_settings.cache_clear()
    try:
        assert get_settings() is get_settings()
    finally:
        get_settings.cache_clear()


# -- settings-api -------------------------------------------------------------

SETTINGS_API_TOKEN = "settings-api-token-for-spotify-api-01"


def test_settings_api_is_off_when_neither_is_set() -> None:
    assert make_settings().settings_api is None


def test_settings_api_is_on_when_both_are_set() -> None:
    settings = make_settings(
        settings_api_base_url="https://settings.test", settings_api_token=SETTINGS_API_TOKEN
    )
    assert settings.settings_api is not None
    base_url, token = settings.settings_api
    assert base_url == "https://settings.test"
    assert token.get_secret_value() == SETTINGS_API_TOKEN


@pytest.mark.parametrize(
    "overrides",
    [
        {"settings_api_base_url": "https://settings.test"},
        {"settings_api_token": SETTINGS_API_TOKEN},
    ],
)
def test_half_a_settings_api_configuration_is_refused(overrides: dict[str, str]) -> None:
    with pytest.raises(ValidationError, match="must be set together"):
        make_settings(**overrides)


def test_a_blank_settings_api_url_means_off() -> None:
    assert make_settings(settings_api_base_url="").settings_api_base_url is None


def test_a_blank_settings_api_token_string_means_off() -> None:
    assert make_settings(settings_api_token="").settings_api_token is None


def test_an_empty_settings_api_token_object_means_off() -> None:
    settings = make_settings(settings_api_token=SecretStr(""))
    assert settings.settings_api_token is None


def test_a_short_settings_api_token_is_refused_without_being_echoed() -> None:
    with pytest.raises(ValidationError) as caught:
        make_settings(
            settings_api_base_url="https://settings.test", settings_api_token="short-token"
        )
    messages = [error["msg"] for error in caught.value.errors()]
    assert messages
    assert all("short-token" not in message for message in messages)


def test_the_settings_api_token_never_appears_in_repr_or_dump() -> None:
    settings = make_settings(
        settings_api_base_url="https://settings.test", settings_api_token=SETTINGS_API_TOKEN
    )
    rendered = f"{settings!r} {settings.model_dump()} {settings.model_dump_json()}"
    assert SETTINGS_API_TOKEN not in rendered


def test_the_settings_api_variable_names_are_recognised() -> None:
    names = known_env_names()
    assert "SPOTIFY_API_SETTINGS_API_BASE_URL" in names
    assert "SPOTIFY_API_SETTINGS_API_TOKEN" in names
