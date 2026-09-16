"""Shared fixtures.

Recorded Spotify payloads live in ``tests/fixtures`` and are loaded here so that
unit, integration and contract tests all assert against the same bytes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict[str, Any]:
    """Load a recorded Spotify response by file stem."""
    payload: dict[str, Any] = json.loads((FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8"))
    return payload


def log_records(out: str) -> list[dict[str, Any]]:
    """Every JSON log record in captured output, parsed. Anything else on stdout is skipped."""
    return [json.loads(line) for line in out.splitlines() if line.startswith("{")]


@pytest.fixture
def search_found() -> dict[str, Any]:
    """A search response containing exactly one track."""
    return load_fixture("search_track_found")


@pytest.fixture
def search_empty() -> dict[str, Any]:
    """A search response containing no tracks at all."""
    return load_fixture("search_track_empty")


@pytest.fixture
def token_response() -> dict[str, Any]:
    """A successful Client Credentials token grant."""
    return {"access_token": "test-access-token", "token_type": "Bearer", "expires_in": 3600}
