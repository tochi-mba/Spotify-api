"""Typed constructors for test fixtures.

``Settings`` normally sources its credentials from the environment, which the
type checker cannot see. Building settings through :func:`make_settings` keeps
tests both type-safe and explicit about what they are overriding.
"""

from __future__ import annotations

from typing import Any

from pydantic import SecretStr

from spotify_api.config import Settings

__all__ = ["TEST_SERVICE_TOKEN", "make_settings"]

TEST_SERVICE_TOKEN = "test-service-token"


def make_settings(**overrides: Any) -> Settings:
    """Build a ``Settings`` instance with dummy credentials and no ``.env``."""
    values: dict[str, Any] = {
        "keyring_base_url": "https://keyring.test",
        "keyring_service_token": SecretStr(TEST_SERVICE_TOKEN),
        "_env_file": None,
    }
    values.update(overrides)
    return Settings(**values)
