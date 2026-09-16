"""Application configuration.

Every setting is an environment variable prefixed ``SPOTIFY_API_`` -- ``SPOTIFY_API_MAX_RETRIES=5``
-- read from the process environment and an optional ``.env`` file exactly once at startup,
and immutable thereafter. Credentials are held as :class:`~pydantic.SecretStr` so that an
accidental ``repr()``, log line or JSON dump cannot leak them.

Two refusals happen before any setting is parsed, in :func:`check_for_unknown_env_vars`:

* a ``SPOTIFY_API_`` variable that names no setting, so a typo is a startup error rather than a
  default silently left in place; and
* a bare name this service read before it had a prefix, such as ``KEYRING_BASE_URL`` or
  ``DEFAULT_MARKET``, reported with the name that replaced it. The bare ``KEYRING_*`` names
  sat inside keyring's own prefix, and keyring refuses variables it does not recognise, so a
  host that configured both services could not start keyring at all.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import TYPE_CHECKING, Annotated, Any, Literal, Self

# Config validation: the same token-length and audience rules keyring enforces, so a
# deployment that could never work fails at startup rather than at the first request.
from keyring_client import ExactAudience, check_service_token
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from spotify_api import SERVICE_NAME

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "ENV_PREFIX",
    "LEGACY_NAMES",
    "MARKET_CODE_LENGTH",
    "Settings",
    "UnknownSettingError",
    "check_for_unknown_env_vars",
    "get_settings",
    "known_env_names",
]

ENV_PREFIX = "SPOTIFY_API_"

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
LogFormat = Literal["json", "console"]
Environment = Literal["development", "test", "staging", "production"]

#: Length of an ISO 3166-1 alpha-2 country code.
MARKET_CODE_LENGTH = 2

LEGACY_NAMES: dict[str, str] = {
    "KEYRING_BASE_URL": "SPOTIFY_API_KEYRING_BASE_URL",
    "KEYRING_SERVICE_TOKEN": "SPOTIFY_API_KEYRING_SERVICE_TOKEN",
    "KEYRING_DEFAULT_PROFILE": "SPOTIFY_API_KEYRING_DEFAULT_PROFILE",
    "KEYRING_TIMEOUT_SECONDS": "SPOTIFY_API_KEYRING_TIMEOUT_SECONDS",
    "CREDENTIAL_CACHE_SKEW_SECONDS": "SPOTIFY_API_CREDENTIAL_CACHE_SKEW_SECONDS",
    "CREDENTIAL_CACHE_DEFAULT_TTL_SECONDS": "SPOTIFY_API_CREDENTIAL_CACHE_DEFAULT_TTL_SECONDS",
    "SPOTIFY_API_BASE_URL": "SPOTIFY_API_SPOTIFY_BASE_URL",
    "MAX_BATCH_SIZE": "SPOTIFY_API_MAX_BATCH_SIZE",
    "CONFIRM_TIMEOUT_SECONDS": "SPOTIFY_API_CONFIRM_TIMEOUT_SECONDS",
    "CONFIRM_POLL_INTERVAL_SECONDS": "SPOTIFY_API_CONFIRM_POLL_INTERVAL_SECONDS",
    "JOB_TTL_SECONDS": "SPOTIFY_API_JOB_TTL_SECONDS",
    "DEFAULT_MARKET": "SPOTIFY_API_DEFAULT_MARKET",
}
"""Names this service read before it had a prefix, and the names that replaced them.

Only names distinctive enough to be this service's own. ``ENVIRONMENT``, ``LOG_LEVEL``,
``MAX_RETRIES`` and the other generic names this service also used to read are left alone,
because other software on the same host legitimately sets them; the changelog lists them.
"""


class Settings(BaseSettings):
    """Runtime configuration for the service."""

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
        frozen=True,
    )

    # --- Keyring -----------------------------------------------------------
    # This service holds no Spotify credential. It verifies the caller's keyring token
    # locally, then asks keyring for the headers to attach, per user, per request.
    keyring_base_url: str = Field(description="Base URL of keyring.")
    keyring_service_token: SecretStr = Field(
        description=(
            "This service's own keyring token, proving which service is asking. It is this "
            "service's entry in keyring's KEYRING_SERVICE_TOKENS, at least 32 characters."
        ),
    )
    keyring_issuer: str = Field(
        default="http://127.0.0.1:8001",
        description="The iss every user token must carry. Must equal keyring's KEYRING_ISSUER.",
    )
    keyring_audience: str = Field(
        default=SERVICE_NAME,
        description=(
            "The aud every user token must carry. Keyring's internal endpoint requires it to "
            "equal this service's name in KEYRING_SERVICE_TOKENS, so change both or neither."
        ),
    )
    keyring_default_profile: str = Field(
        default="personal",
        description="Profile used when a request does not name one.",
    )
    keyring_timeout_seconds: Annotated[float, Field(gt=0, le=60)] = 5.0
    jwks_cache_seconds: Annotated[float, Field(gt=0, le=86_400)] = 3600.0
    jwks_min_refetch_seconds: Annotated[float, Field(gt=0, le=3600)] = 60.0
    credential_cache_skew_seconds: Annotated[int, Field(ge=0, le=600)] = 60
    credential_cache_default_ttl_seconds: Annotated[int, Field(ge=0, le=3600)] = 300

    # --- Per-person settings -----------------------------------------------
    settings_api_base_url: str | None = Field(
        default=None,
        description=(
            "Where settings-api is. Unset, every person gets this configuration as it stands. "
            "Set, each request reads its owner's spotify settings: default market, lookup "
            "batch cap, confirm timeout, and which profile they mean when they name none."
        ),
    )
    settings_api_token: SecretStr | None = Field(
        default=None,
        description=(
            "This service's entry in settings-api's SETTINGS_API_SERVICES. At least 32 "
            "characters. Its grant there needs audience_prefix spotify-api: settings-api is "
            "shown the same user token keyring minted."
        ),
    )

    # --- Upstream endpoints ------------------------------------------------
    spotify_base_url: str = Field(
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

    @field_validator("spotify_base_url", "keyring_base_url")
    @classmethod
    def _strip_trailing_slashes(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("keyring_service_token")
    @classmethod
    def _check_service_token(cls, value: SecretStr) -> SecretStr:
        """Refuse a token keyring would refuse, at startup rather than at the first request."""
        check_service_token(value.get_secret_value())
        return value

    @field_validator("keyring_audience")
    @classmethod
    def _check_audience(cls, value: str) -> str:
        """Refuse an audience keyring could never mint: empty, untrimmed, or with a dot."""
        ExactAudience(value)
        return value

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

    @field_validator("settings_api_base_url")
    @classmethod
    def _blank_settings_url_is_unset(cls, value: str | None) -> str | None:
        """``SPOTIFY_API_SETTINGS_API_BASE_URL=`` in a ``.env`` means off, not an empty URL."""
        return value or None

    @field_validator("settings_api_token", mode="before")
    @classmethod
    def _blank_settings_token_is_unset(cls, value: Any) -> Any:  # noqa: ANN401
        if value is None or value == "":
            return None
        if isinstance(value, SecretStr) and not value.get_secret_value():
            return None
        return value

    @field_validator("settings_api_token")
    @classmethod
    def _check_settings_api_token(cls, value: SecretStr | None) -> SecretStr | None:
        """Refuse a token settings-api would refuse, at startup rather than at the first request."""
        if value is not None:
            check_service_token(value.get_secret_value())
        return value

    @model_validator(mode="after")
    def _check_settings_api_is_whole(self) -> Self:
        """Refuse half a settings-api configuration.

        A URL with no token would be refused on every call, and a token with no URL is a
        secret configured for nothing. Either is somebody's mistake, and startup is the
        cheapest place to hear about it.
        """
        if (self.settings_api_base_url is None) != (self.settings_api_token is None):
            message = "settings_api_base_url and settings_api_token must be set together"
            raise ValueError(message)
        return self

    @property
    def settings_api(self) -> tuple[str, SecretStr] | None:
        """Where settings-api is and how to authenticate to it, or ``None`` when unused.

        One value rather than two optional ones, so that nothing downstream has to
        re-establish that the pair is whole: :meth:`_check_settings_api_is_whole` already
        refused to construct settings where it is not.
        """
        if self.settings_api_base_url is None or self.settings_api_token is None:
            return None
        return self.settings_api_base_url, self.settings_api_token

    @property
    def is_production(self) -> bool:
        """Whether the service is running in its production environment."""
        return self.environment == "production"


class UnknownSettingError(ValueError):
    """A variable is set that no setting corresponds to, or that has since been renamed."""


def known_env_names() -> frozenset[str]:
    """Every environment variable name this configuration reads."""
    return frozenset(f"{ENV_PREFIX}{name.upper()}" for name in Settings.model_fields)


def check_for_unknown_env_vars(environ: Mapping[str, str] | None = None) -> None:
    """Fail on a misspelled or renamed setting instead of quietly running with a default.

    Raises:
        UnknownSettingError: naming every offender at once, each renamed one with its
            replacement, so a deployment is fixed in one pass. Values are never echoed.
    """
    present = os.environ if environ is None else environ
    known = known_env_names()
    offenders = sorted(
        name
        for name in present
        if name not in known and (name.startswith(ENV_PREFIX) or name in LEGACY_NAMES)
    )
    if offenders:
        described = ", ".join(
            f"{name} (now {LEGACY_NAMES[name]})" if name in LEGACY_NAMES else name
            for name in offenders
        )
        message = (
            f"unrecognised configuration: {described}. Every setting is prefixed "
            f"{ENV_PREFIX}; see .env.example."
        )
        raise UnknownSettingError(message)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    Cached so that configuration is parsed once. Tests call ``get_settings.cache_clear()``
    to force a reload.

    Raises:
        UnknownSettingError: see :func:`check_for_unknown_env_vars`.
    """
    check_for_unknown_env_vars()
    # Credentials are supplied by the environment; mypy cannot see settings sources.
    return Settings()  # type: ignore[call-arg]
