"""Domain exceptions.

Every failure this service raises deliberately is one of these. Each carries a stable,
machine-readable ``error_type`` -- recorded on a failed job, and the last segment of the problem
``type`` a caller is shown -- a message written for the caller, and any structured details worth
handing back, such as the player state a confirmation gave up on.

None of them knows about HTTP. Which status each one is rendered as is decided in a single
table in :mod:`spotify_api.api.errors`, so the Spotify adapter, the credential provider and the
job runner raise them without importing the web framework, and a new failure cannot quietly
choose a status of its own.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "BatchTooLargeError",
    "ConfirmationTimeoutError",
    "CredentialUnavailableError",
    "JobNotFoundError",
    "KeyringUnavailableError",
    "NoActiveDeviceError",
    "PreferencesUnavailableError",
    "PremiumRequiredError",
    "ServiceError",
    "SpotifyAuthError",
    "SpotifyRateLimitError",
    "SpotifyUnavailableError",
    "TrackLookupError",
    "UserTokenRejectedError",
]


class ServiceError(Exception):
    """Base class for every failure this service raises deliberately."""

    #: Stable, machine-readable discriminator: a failed job's ``error_type``, and the problem type.
    error_type: str = "service_error"

    def __init__(self, message: str, **details: Any) -> None:  # noqa: ANN401
        """Store ``message`` and any structured ``details`` worth handing back.

        Both reach the caller, so neither may carry an exception's text, a URL or a credential.
        ``None`` details are dropped rather than rendered as nulls.
        """
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = {k: v for k, v in details.items() if v is not None}


class BatchTooLargeError(ServiceError):
    """The batch exceeded this deployment's configured operational limit."""

    error_type = "batch_too_large"


class ConfirmationTimeoutError(ServiceError):
    """Spotify accepted the command but its effect never became observable.

    Carries the last player state seen, because "it did not work" is much less
    useful than "it is still playing the previous track" or "the device
    reports volume 30, not 80".
    """

    error_type = "confirmation_timeout"

    def __init__(self, message: str, observed: object = None) -> None:
        """Record what the player looked like when we gave up."""
        super().__init__(message, observed=observed)


class JobNotFoundError(ServiceError):
    """No job with that id, or it has aged out of the store.

    Deliberately one error for both: telling a caller that a job *used* to
    exist says something about other callers' traffic.
    """

    error_type = "job_not_found"


class UserTokenRejectedError(ServiceError):
    """The caller's keyring user token is missing, expired, or not for us.

    Distinct from the other credential failures because the caller can fix it
    by presenting a valid token, which is not true of any of them.
    """

    error_type = "user_token_rejected"


class CredentialUnavailableError(ServiceError):
    """keyring has no usable Spotify credential for this user and profile.

    The grant was revoked, never made, or could not be refreshed. Retrying
    cannot help -- the user must reconnect Spotify in keyring.
    """

    error_type = "credential_unavailable"


class KeyringUnavailableError(ServiceError):
    """keyring itself is unreachable or failing.

    Distinct from CredentialUnavailableError on purpose: this one is worth
    retrying, and it points at an operator problem rather than a user one.
    """

    error_type = "keyring_unavailable"


class PreferencesUnavailableError(ServiceError):
    """A person's settings were needed and could not be read honestly.

    Either settings-api refused this service -- a grant it was not given, a token it does
    not recognise -- or it cannot be reached and the setting in question is one that must
    not be guessed at. Neither is the caller's doing, so it is not a 4xx.
    """

    error_type = "preferences_unavailable"


class SpotifyAuthError(ServiceError):
    """Spotify rejected our credentials, or a token could not be obtained."""

    error_type = "spotify_auth_error"


class PremiumRequiredError(ServiceError):
    """The action needs Spotify Premium and this account does not have it.

    Named separately because it is the single most common reason playback
    fails, and "upgrade your account" is a useful thing to be told.
    """

    error_type = "premium_required"


class NoActiveDeviceError(ServiceError):
    """Spotify has no device to act on.

    Playback commands need somewhere to play. Spotify reports this as a bare
    404, which is indistinguishable from a missing resource unless you look at
    the route -- so it is translated here into something actionable.
    """

    error_type = "no_active_device"


class SpotifyRateLimitError(ServiceError):
    """Spotify returned 429 and the retry budget was exhausted."""

    error_type = "spotify_rate_limit_error"

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        """Record how long Spotify asked us to wait, when it told us."""
        super().__init__(message, retry_after=retry_after)
        self.retry_after = retry_after


class SpotifyUnavailableError(ServiceError):
    """Spotify was unreachable, timed out, or kept returning 5xx."""

    error_type = "spotify_unavailable_error"


class TrackLookupError(ServiceError):
    """A single item could not be resolved for a reason we can describe."""

    error_type = "track_lookup_error"
