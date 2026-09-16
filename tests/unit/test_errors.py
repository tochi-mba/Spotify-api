"""The domain exceptions carry what a caller is told, and nothing about HTTP."""

from __future__ import annotations

import pytest

from spotify_api.errors import (
    BatchTooLargeError,
    ConfirmationTimeoutError,
    CredentialUnavailableError,
    JobNotFoundError,
    KeyringUnavailableError,
    NoActiveDeviceError,
    PreferencesUnavailableError,
    PremiumRequiredError,
    ServiceError,
    SpotifyAuthError,
    SpotifyRateLimitError,
    SpotifyUnavailableError,
    TrackLookupError,
    UserTokenRejectedError,
)


def test_service_error_carries_message_and_type() -> None:
    error = ServiceError("something went wrong")
    assert str(error) == "something went wrong"
    assert error.message == "something went wrong"
    assert error.error_type == "service_error"
    assert error.details == {}


def test_details_are_preserved() -> None:
    error = SpotifyRateLimitError("slow down", retry_after=7.5)
    assert error.details == {"retry_after": 7.5}
    assert error.retry_after == 7.5


def test_absent_details_are_dropped_rather_than_rendered_as_nulls() -> None:
    error = SpotifyUnavailableError("down", upstream_status=None, upstream_detail="try later")
    assert error.details == {"upstream_detail": "try later"}


def test_a_confirmation_timeout_keeps_what_the_player_looked_like() -> None:
    observed = {"is_playing": False}
    assert ConfirmationTimeoutError("gave up", observed=observed).details == {"observed": observed}


@pytest.mark.parametrize(
    ("exc", "error_type"),
    [
        (BatchTooLargeError("too many"), "batch_too_large"),
        (ConfirmationTimeoutError("gave up"), "confirmation_timeout"),
        (CredentialUnavailableError("not connected"), "credential_unavailable"),
        (JobNotFoundError("no such job"), "job_not_found"),
        (KeyringUnavailableError("keyring down"), "keyring_unavailable"),
        (NoActiveDeviceError("no device"), "no_active_device"),
        (PreferencesUnavailableError("settings-api refused"), "preferences_unavailable"),
        (PremiumRequiredError("upgrade"), "premium_required"),
        (SpotifyAuthError("bad creds"), "spotify_auth_error"),
        (SpotifyRateLimitError("429"), "spotify_rate_limit_error"),
        (SpotifyUnavailableError("upstream down"), "spotify_unavailable_error"),
        (TrackLookupError("no luck"), "track_lookup_error"),
        (UserTokenRejectedError("refused"), "user_token_rejected"),
    ],
)
def test_subclasses_declare_their_own_stable_type(exc: ServiceError, error_type: str) -> None:
    # Recorded on failed jobs and published as the problem type, so these are wire values.
    assert exc.error_type == error_type
    assert isinstance(exc, ServiceError)


def test_rate_limit_error_without_retry_after_has_no_details() -> None:
    assert SpotifyRateLimitError("429").details == {}
