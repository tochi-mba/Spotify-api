"""Typed constructors for test fixtures.

``Settings`` normally sources its credentials from the environment, which the
type checker cannot see. Building settings through :func:`make_settings` keeps
tests both type-safe and explicit about what they are overriding.
"""

from __future__ import annotations

from typing import Any

from pydantic import SecretStr

from spotify_api.config import Settings

__all__ = ["TEST_CLIENT_ID", "TEST_CLIENT_SECRET", "make_settings"]

TEST_CLIENT_ID = "test-client-id"
TEST_CLIENT_SECRET = "test-client-secret"


def make_settings(**overrides: Any) -> Settings:
    """Build a ``Settings`` instance with dummy credentials and no ``.env``."""
    values: dict[str, Any] = {
        "spotify_client_id": SecretStr(TEST_CLIENT_ID),
        "spotify_client_secret": SecretStr(TEST_CLIENT_SECRET),
        "_env_file": None,
    }
    values.update(overrides)
    return Settings(**values)
