"""Typed constructors for test fixtures.

``Settings`` normally sources its credentials from the environment, which the type checker
cannot see. Building settings through :func:`make_settings` keeps tests both type-safe and
explicit about what they are overriding.

Keyring is faked with :mod:`keyring_client.testing`, the one fake every service in the family
shares: a real RSA key, a real JWKS document, and tokens signed the way keyring signs them.
"""

from __future__ import annotations

from typing import Any

from keyring_client.testing import BASE_URL, ISSUER, mint
from pydantic import SecretStr

from spotify_api.api.errors import PROBLEM_BASE_URI
from spotify_api.config import Settings

__all__ = [
    "AUDIENCE",
    "KEYRING_BASE_URL",
    "TEST_SERVICE_TOKEN",
    "make_settings",
    "problem_type",
    "user_token",
]

TEST_SERVICE_TOKEN = "test-service-token-0123456789abcdef"

AUDIENCE = "spotify-api"
"""What keyring mints this service's user tokens for: its name in KEYRING_SERVICE_TOKENS."""

KEYRING_BASE_URL = BASE_URL


def make_settings(**overrides: Any) -> Settings:
    """Build a ``Settings`` instance pointed at the fake keyring, with no ``.env``."""
    values: dict[str, Any] = {
        "keyring_base_url": KEYRING_BASE_URL,
        "keyring_service_token": SecretStr(TEST_SERVICE_TOKEN),
        "keyring_issuer": ISSUER,
        "_env_file": None,
    }
    values.update(overrides)
    return Settings(**values)


def user_token(account_id: str = "account-a", **overrides: Any) -> str:
    """A user token keyring would mint for this service, for ``account_id``."""
    return mint(account_id=account_id, audience=AUDIENCE, issuer=ISSUER, **overrides)


def problem_type(slug: str) -> str:
    """The ``type`` a problem document carries for ``slug``, such as ``no-active-device``."""
    return f"{PROBLEM_BASE_URI}/{slug}"
