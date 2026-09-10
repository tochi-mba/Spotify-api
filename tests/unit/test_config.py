"""Settings load from the environment, validate hard, and never leak secrets."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from spotify_api.config import Settings, get_settings
from tests.factories import make_settings


@pytest.fixture
def base_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KEYRING_BASE_URL", "https://keyring.from-env")
    monkeypatch.setenv("KEYRING_SERVICE_TOKEN", "service-token-from-env")


@pytest.mark.usefixtures("base_env")
def test_reads_keyring_configuration_from_the_environment() -> None:
    # Supplied by the environment, which mypy cannot see.
    settings = Settings()  # type: ignore[call-arg]
    assert settings.keyring_base_url == "https://keyring.from-env"
    assert settings.keyring_service_token.get_secret_value() == "service-token-from-env"


@pytest.mark.usefixtures("base_env")
def test_operational_defaults_are_sane() -> None:
    settings = Settings()  # type: ignore[call-arg]
    assert settings.environment == "development"
    assert settings.log_level == "INFO"
    assert settings.log_format == "json"
    assert settings.spotify_api_base_url == "https://api.spotify.com/v1"
    assert settings.keyring_default_profile == "personal"
    assert settings.keyring_timeout_seconds == 5.0
    assert settings.credential_cache_skew_seconds == 60
    assert settings.credential_cache_default_ttl_seconds == 300
    assert settings.request_timeout_seconds == 10.0
    assert settings.max_retries == 3
    assert settings.retry_backoff_base_seconds == 0.2
    assert settings.max_concurrency == 8
    assert settings.max_batch_size == 50
    assert settings.default_market is None


def test_missing_keyring_configuration_is_a_startup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KEYRING_BASE_URL", raising=False)
    monkeypatch.delenv("KEYRING_SERVICE_TOKEN", raising=False)
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)  # type: ignore[call-arg]
    missing = {error["loc"][0] for error in excinfo.value.errors()}
    assert missing == {"keyring_base_url", "keyring_service_token"}


@pytest.mark.usefixtures("base_env")
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
    ],
)
def test_out_of_range_values_are_rejected(field: str, value: float) -> None:
    with pytest.raises(ValidationError):
        make_settings(**{field: value})


@pytest.mark.usefixtures("base_env")
def test_trailing_slashes_are_stripped_from_base_urls() -> None:
    settings = make_settings(
        spotify_api_base_url="https://example.test/v1/",
        keyring_base_url="https://keyring.example.test//",
    )
    assert settings.spotify_api_base_url == "https://example.test/v1"
    assert settings.keyring_base_url == "https://keyring.example.test"


@pytest.mark.usefixtures("base_env")
@pytest.mark.parametrize(("given", "expected"), [("gb", "GB"), ("  us  ", "US"), ("", None)])
def test_default_market_is_normalised(given: str, expected: str | None) -> None:
    assert make_settings(default_market=given).default_market == expected


@pytest.mark.usefixtures("base_env")
def test_unknown_market_code_is_rejected() -> None:
    with pytest.raises(ValidationError):
        make_settings(default_market="GBR")


@pytest.mark.usefixtures("base_env")
def test_the_service_token_never_appears_in_repr_or_dump() -> None:
    settings = Settings()  # type: ignore[call-arg]
    rendered = f"{settings!r} {settings.model_dump()} {settings.model_dump_json()}"
    assert "service-token-from-env" not in rendered
    assert "**********" in repr(settings)


@pytest.mark.usefixtures("base_env")
def test_is_production_reflects_the_environment() -> None:
    assert make_settings(environment="production").is_production is True
    assert make_settings(environment="development").is_production is False


@pytest.mark.usefixtures("base_env")
def test_get_settings_is_cached() -> None:
    get_settings.cache_clear()
    assert get_settings() is get_settings()
    get_settings.cache_clear()
