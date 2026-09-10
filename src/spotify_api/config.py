"""Application configuration.

Settings are read from the process environment (and an optional ``.env`` file)
exactly once at startup and are immutable thereafter. Credentials are held as
:class:`~pydantic.SecretStr` so that they cannot be leaked by an accidental
``repr()``, log line or JSON dump.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["Settings", "get_settings"]

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
LogFormat = Literal["json", "console"]
Environment = Literal["development", "test", "staging", "production"]

#: Length of an ISO 3166-1 alpha-2 country code.
MARKET_CODE_LENGTH = 2


class Settings(BaseSettings):
    """Runtime configuration for the service.

    Every field is overridable by an environment variable of the same name,
    upper-cased (for example ``MAX_RETRIES=5``).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    # --- Credentials -------------------------------------------------------
    # This service holds no Spotify credential. It asks keyring for the headers
    # to attach, per user, per request. See docs/adr/0005-keyring-credentials.md.
    keyring_base_url: str = Field(
        description="Base URL of the keyring credentials service.",
    )
    keyring_service_token: SecretStr = Field(
        description="This service's own keyring token, proving which service is asking.",
    )
    keyring_default_profile: str = Field(
        default="personal",
        description="Profile used when a request does not name one.",
    )
    keyring_timeout_seconds: Annotated[float, Field(gt=0, le=60)] = 5.0
    credential_cache_skew_seconds: Annotated[int, Field(ge=0, le=600)] = 60
    credential_cache_default_ttl_seconds: Annotated[int, Field(ge=0, le=3600)] = 300

    # --- Upstream endpoints ------------------------------------------------
    spotify_api_base_url: str = Field(
        default="https://api.spotify.com/v1",
        description="Base URL of the Spotify Web API. Overridable for tests and sandboxes.",
    )

    # --- Resilience --------------------------------------------------------
    request_timeout_seconds: Annotated[float, Field(gt=0, le=120)] = 10.0
    max_retries: Annotated[int, Field(ge=0, le=10)] = 3
    retry_backoff_base_seconds: Annotated[float, Field(ge=0, le=10)] = 0.2
    max_concurrency: Annotated[int, Field(ge=1, le=64)] = 8
    max_batch_size: Annotated[int, Field(ge=1, le=200)] = 50

    # --- Async jobs --------------------------------------------------------
    confirm_timeout_seconds: Annotated[float, Field(gt=0, le=300)] = 15.0
    confirm_poll_interval_seconds: Annotated[float, Field(gt=0, le=30)] = 0.5
    job_ttl_seconds: Annotated[float, Field(gt=0, le=86400)] = 3600.0

    # --- Behaviour ---------------------------------------------------------
    default_market: str | None = Field(
        default=None,
        description="ISO 3166-1 alpha-2 market applied when a request omits one.",
    )

    # --- Observability -----------------------------------------------------
    environment: Environment = "development"
    log_level: LogLevel = "INFO"
    log_format: LogFormat = "json"

    @field_validator("spotify_api_base_url", "keyring_base_url")
    @classmethod
    def _strip_trailing_slashes(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("default_market", mode="before")
    @classmethod
    def _normalise_market(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalised = value.strip().upper()
        return normalised or None

    @field_validator("default_market")
    @classmethod
    def _validate_market(cls, value: str | None) -> str | None:
        if value is not None and not (len(value) == MARKET_CODE_LENGTH and value.isalpha()):
            message = "market must be an ISO 3166-1 alpha-2 country code, e.g. 'GB'"
            raise ValueError(message)
        return value

    @property
    def is_production(self) -> bool:
        """Whether the service is running in its production environment."""
        return self.environment == "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    Cached so that configuration is parsed once. Tests call
    ``get_settings.cache_clear()`` to force a reload.
    """
    # Credentials are supplied by the environment; mypy cannot see settings sources.
    return Settings()  # type: ignore[call-arg]
