"""The domain exception hierarchy maps cleanly onto HTTP semantics."""

from __future__ import annotations

import pytest

from spotify_api.errors import (
    ServiceError,
    SpotifyAuthError,
    SpotifyRateLimitError,
    SpotifyUnavailableError,
    TrackLookupError,
)


def test_service_error_carries_message_type_and_status() -> None:
    error = ServiceError("something went wrong")
    assert str(error) == "something went wrong"
    assert error.message == "something went wrong"
    assert error.error_type == "service_error"
    assert error.status_code == 500
    assert error.details == {}


def test_details_are_preserved() -> None:
    error = SpotifyRateLimitError("slow down", retry_after=7.5)
    assert error.details == {"retry_after": 7.5}
    assert error.retry_after == 7.5


@pytest.mark.parametrize(
    ("exc", "error_type", "status"),
    [
        (SpotifyAuthError("bad creds"), "spotify_auth_error", 503),
        (SpotifyRateLimitError("429"), "spotify_rate_limit_error", 503),
        (SpotifyUnavailableError("upstream down"), "spotify_unavailable_error", 503),
        (TrackLookupError("no luck"), "track_lookup_error", 502),
    ],
)
def test_subclasses_declare_their_own_type_and_status(
    exc: ServiceError, error_type: str, status: int
) -> None:
    assert exc.error_type == error_type
    assert exc.status_code == status
    assert isinstance(exc, ServiceError)


def test_rate_limit_error_without_retry_after_has_no_details() -> None:
    assert SpotifyRateLimitError("429").details == {}


def test_to_payload_matches_the_public_error_envelope() -> None:
    payload = SpotifyAuthError("bad creds").to_payload(request_id="req-1")
    assert payload == {
        "error": {
            "type": "spotify_auth_error",
            "message": "bad creds",
            "details": {},
            "request_id": "req-1",
        }
    }
