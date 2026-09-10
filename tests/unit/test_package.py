"""The package exposes a version, and the coverage gate is live from commit one."""

from __future__ import annotations

import re

import spotify_api


def test_package_exposes_a_semver_version() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", spotify_api.__version__)


def test_service_name_is_stable() -> None:
    assert spotify_api.SERVICE_NAME == "spotify-api"
