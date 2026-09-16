"""What one person has chosen, and what this service does when it cannot ask.

The deployment's configuration says how spotify-api behaves for everybody.
settings-api holds what each person has chosen within that, and this module is
the one place the two meet. Nothing is read at startup, and with no settings-api
configured every person gets the configuration as it stands -- exactly what the
service did before it read anybody's settings at all.

Three rules shape it.

**A person may narrow a ceiling and never raise it.** ``max_batch_size`` and
``confirm_timeout_seconds`` are clamped to this deployment's caps. The catalogue
stores the timeout as an integer; this service uses a float -- conversion happens
here, once.

**An outage degrades per setting.** Keys that fall back use the configuration
(or settings-api's own defaults, once this client has seen them). ``default_profile``
refuses: guessing ``personal`` would quietly act on the wrong account. It fails
only the request that named no profile.

**A refusal is not an outage.** settings-api answering 401 or 403 means this
service is misconfigured, and serving defaults would hide that behind behaviour
that happens to work. The request fails instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from settings_client import (
    HttpSettingsClient,
    SettingsRefused,
    SettingsRejected,
    SettingsUnavailable,
)

from spotify_api.config import MARKET_CODE_LENGTH, Settings
from spotify_api.errors import PreferencesUnavailableError
from spotify_api.logging import get_logger

if TYPE_CHECKING:
    from settings_client import ResolvedSettings, SettingsClient

logger = get_logger(__name__)

NAMESPACE = "spotify"

PROFILE_UNKNOWN = (
    "your default profile could not be read from settings-api; name a profile in the "
    "request, or try again shortly"
)
REFUSED = "settings-api did not accept this service's request for your settings"
NOT_GUESSED = "one of your settings could not be read from settings-api and must not be guessed"


@dataclass(frozen=True, slots=True)
class Preferences:
    """One person's choices, as this service applies them to one request."""

    default_market: str | None
    """ISO 3166-1 alpha-2 market used when a lookup body omits one. ``None`` lets Spotify decide."""

    max_batch_size: int
    """How many tracks one lookup may carry, inside the deployment cap."""

    confirm_timeout_seconds: float
    """How long a pending action waits for confirmation, captured when the job is submitted."""

    default_profile: str | None
    """The profile a request uses when it names none.

    ``None`` when settings-api could not be asked and the answer must not be guessed.
    """

    def profile(self, requested: str | None) -> str:
        """The profile a request is resolved with: the one it named, or the default.

        Raises:
            PreferencesUnavailableError: the request named none and the default is unknown.
        """
        if requested is not None:
            return requested
        if self.default_profile is None:
            raise PreferencesUnavailableError(PROFILE_UNKNOWN)
        return self.default_profile


class PreferenceSource(Protocol):
    """Where a request's preferences come from."""

    async def for_token(self, user_token: str | None, /) -> Preferences:
        """The preferences of whoever ``user_token`` belongs to.

        ``None`` is no caller at all -- a path that does not authenticate -- and gets the
        configuration.

        Raises:
            PreferencesUnavailableError: settings-api refused this service, or cannot
                read a setting that must not be guessed.
        """
        ...

    async def aclose(self) -> None:
        """Release whatever this holds open."""
        ...


def deployment_preferences(settings: Settings) -> Preferences:
    """What everybody gets when nobody's own choices are known: the configuration as it is."""
    return Preferences(
        default_market=settings.default_market,
        max_batch_size=settings.max_batch_size,
        confirm_timeout_seconds=settings.confirm_timeout_seconds,
        default_profile=settings.keyring_default_profile,
    )


class DeploymentPreferences:
    """Everybody gets the configuration: what this service did before it read settings-api."""

    def __init__(self, settings: Settings) -> None:
        """Bind the configuration everybody gets."""
        self._preferences = deployment_preferences(settings)

    async def for_token(self, _user_token: str | None, /) -> Preferences:
        """Return the configuration; the token is ignored."""
        return self._preferences

    async def aclose(self) -> None:
        """Nothing is held open."""


class SettingsApiPreferences:
    """Each person's own choices, read from settings-api, inside the deployment's ceilings."""

    def __init__(self, *, client: SettingsClient, settings: Settings) -> None:
        """Bind the client and the deployment ceilings it is read inside."""
        self._client = client
        self._settings = settings
        self._deployment = deployment_preferences(settings)

    async def for_token(self, user_token: str | None, /) -> Preferences:
        """Read this person's ``spotify`` settings, or the configuration if there is none."""
        if user_token is None:
            return self._deployment

        try:
            resolved = await self._client.resolve(NAMESPACE, user_token=user_token)
        except SettingsUnavailable:
            logger.warning("settings_unavailable", namespace=NAMESPACE)
            return Preferences(
                default_market=self._deployment.default_market,
                max_batch_size=self._deployment.max_batch_size,
                confirm_timeout_seconds=self._deployment.confirm_timeout_seconds,
                default_profile=None,
            )
        except SettingsRejected as error:
            logger.warning("settings_rejected", namespace=NAMESPACE, status_code=error.status_code)
            raise PreferencesUnavailableError(REFUSED) from error

        if resolved.stale:
            logger.info("settings_stale", namespace=NAMESPACE)

        try:
            return self._apply(resolved)
        except SettingsRefused as error:
            logger.warning("settings_refused", namespace=NAMESPACE, key=error.key)
            raise PreferencesUnavailableError(NOT_GUESSED) from error

    async def aclose(self) -> None:
        """Close the settings-api client."""
        await self._client.aclose()

    def _apply(self, resolved: ResolvedSettings) -> Preferences:
        """Turn one person's resolved namespace into what a request runs under."""
        deployment = self._deployment
        batch = _whole_number(resolved, "max_batch_size", minimum=1)
        timeout = _positive_seconds(resolved, "confirm_timeout_seconds")
        return Preferences(
            default_market=self._market(resolved),
            max_batch_size=_narrow_int(deployment.max_batch_size, batch),
            confirm_timeout_seconds=_narrow_float(deployment.confirm_timeout_seconds, timeout),
            default_profile=self._default_profile(resolved),
        )

    def _market(self, resolved: ResolvedSettings) -> str | None:
        """``spotify.default_market``: a country code, null, or the configuration."""
        if "default_market" not in resolved:
            return self._deployment.default_market
        value = resolved.get("default_market", None)
        if value is None:
            return None
        if isinstance(value, str):
            normalised = value.strip().upper() or None
            if normalised is None:
                return None
            if len(normalised) == MARKET_CODE_LENGTH and normalised.isalpha():
                return normalised
        logger.warning("setting_unusable", namespace=NAMESPACE, key="default_market")
        return self._deployment.default_market

    def _default_profile(self, resolved: ResolvedSettings) -> str | None:
        """``common.default_profile``: this person's, the configuration's, or unknown."""
        try:
            value = resolved.get("default_profile", None)
        except SettingsRefused:
            return None
        if isinstance(value, str) and value:
            return value
        if value is not None:
            logger.warning("setting_unusable", namespace="common", key="default_profile")
        return self._settings.keyring_default_profile


def build_preference_source(
    settings: Settings, *, client: SettingsClient | None = None
) -> PreferenceSource:
    """Choose where preferences come from, and say which in the log.

    Args:
        settings: the configuration, which also says whether settings-api is in use.
        client: substituted by tests with :class:`settings_client.testing.FakeSettingsClient`,
            and used in place of building one from ``settings``.
    """
    if client is None:
        configured = settings.settings_api
        if configured is None:
            logger.info("per_person_settings_off")
            return DeploymentPreferences(settings)
        base_url, token = configured
        client = HttpSettingsClient(base_url=base_url, service_token=token.get_secret_value())

    logger.info("per_person_settings_on", namespace=NAMESPACE)
    return SettingsApiPreferences(client=client, settings=settings)


def _narrow_int(deployment: int, chosen: int | None) -> int:
    """The deployment's cap, or the person's if they asked for less."""
    return deployment if chosen is None else min(deployment, chosen)


def _narrow_float(deployment: float, chosen: float | None) -> float:
    """The deployment's cap, or the person's if they asked for less."""
    return deployment if chosen is None else min(deployment, chosen)


def _whole_number(resolved: ResolvedSettings, key: str, *, minimum: int) -> int | None:
    """``key`` as a whole number no smaller than ``minimum``, or ``None`` if there is none.

    A deployment running an older settings-api may not have the key, and a value of the
    wrong shape is settings-api's bug rather than a reason to fail somebody's lookup.
    Either way the configuration stands in. The key is logged; the value never is.
    """
    value = resolved.get(key, None)
    if isinstance(value, int) and not isinstance(value, bool) and value >= minimum:
        return value
    if value is not None:
        logger.warning("setting_unusable", namespace=NAMESPACE, key=key)
    return None


def _positive_seconds(resolved: ResolvedSettings, key: str) -> float | None:
    """``key`` as a positive number of seconds.

    The catalogue stores this as an integer; this service's own knob is a float. An int
    is converted here. Any other shape is unusable and the configuration stands in.
    """
    value = resolved.get(key, None)
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return float(value)
    if value is not None:
        logger.warning("setting_unusable", namespace=NAMESPACE, key=key)
    return None


__all__ = [
    "NOT_GUESSED",
    "PROFILE_UNKNOWN",
    "REFUSED",
    "DeploymentPreferences",
    "PreferenceSource",
    "Preferences",
    "SettingsApiPreferences",
    "build_preference_source",
    "deployment_preferences",
]
