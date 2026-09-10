"""Dependency wiring.

Routes ask for a :class:`TrackResolver`, never for a Spotify client. The
concrete graph is assembled once during application startup and stashed on
``app.state``; these providers hand it out. Tests replace either provider
through FastAPI's ``dependency_overrides``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, Header, Request
from pydantic import SecretStr

from spotify_api.credentials.models import UserContext
from spotify_api.errors import UserTokenRejectedError

if TYPE_CHECKING:
    from spotify_api.config import Settings
    from spotify_api.jobs.confirm import PlaybackConfirmer
    from spotify_api.jobs.protocols import JobStore
    from spotify_api.jobs.runner import JobRunner
    from spotify_api.spotify.protocols import TrackResolver
    from spotify_api.spotify.resources.player import PlayerResource

__all__ = [
    "PROFILE_HEADER",
    "USER_TOKEN_HEADER",
    "ConfirmerDep",
    "JobRunnerDep",
    "JobStoreDep",
    "PlayerDep",
    "ResolverDep",
    "SettingsDep",
    "UserContextDep",
    "get_confirmer",
    "get_job_runner",
    "get_job_store",
    "get_player",
    "get_resolver",
    "get_settings_dependency",
    "get_user_context",
]

#: The caller's short-lived keyring token, naming whose account to act on.
USER_TOKEN_HEADER = "X-Keyring-User-Token"  # noqa: S105 -- a header name, not a credential

#: Which keyring profile to read the Spotify connection from.
PROFILE_HEADER = "X-Keyring-Profile"


def get_resolver(request: Request) -> TrackResolver:
    """Return the resolver assembled at startup."""
    resolver: TrackResolver = request.app.state.resolver
    return resolver


def get_player(request: Request) -> PlayerResource:
    """Return the player resource assembled at startup."""
    player: PlayerResource = request.app.state.player
    return player


def get_confirmer(request: Request) -> PlaybackConfirmer:
    """Return the playback confirmer assembled at startup."""
    confirmer: PlaybackConfirmer = request.app.state.confirmer
    return confirmer


def get_job_runner(request: Request) -> JobRunner:
    """Return the job runner assembled at startup."""
    runner: JobRunner = request.app.state.job_runner
    return runner


def get_job_store(request: Request) -> JobStore:
    """Return the job store assembled at startup."""
    store: JobStore = request.app.state.job_store
    return store


def get_settings_dependency(request: Request) -> Settings:
    """Return the settings this application was created with."""
    settings: Settings = request.app.state.settings
    return settings


def get_user_context(
    request: Request,
    x_keyring_user_token: Annotated[
        str | None,
        Header(
            alias=USER_TOKEN_HEADER,
            description=(
                "The caller's short-lived keyring user token, minted by keyring with this "
                "service as its audience. Required: this service holds no Spotify "
                "credential of its own and acts only on a named user's behalf."
            ),
        ),
    ] = None,
    x_keyring_profile: Annotated[
        str | None,
        Header(
            alias=PROFILE_HEADER,
            description="Which keyring profile to use. Defaults to the configured profile.",
        ),
    ] = None,
) -> UserContext:
    """Build the acting-user context from the request headers.

    Raises:
        UserTokenRejectedError: no user token was presented.
    """
    token = (x_keyring_user_token or "").strip()
    if not token:
        message = (
            f"{USER_TOKEN_HEADER} is required; mint one from keyring with this service "
            "as the audience"
        )
        raise UserTokenRejectedError(message)

    settings: Settings = request.app.state.settings
    profile = (x_keyring_profile or "").strip() or settings.keyring_default_profile
    return UserContext(user_token=SecretStr(token), profile=profile)


ConfirmerDep = Annotated["PlaybackConfirmer", Depends(get_confirmer)]
JobRunnerDep = Annotated["JobRunner", Depends(get_job_runner)]
PlayerDep = Annotated["PlayerResource", Depends(get_player)]
JobStoreDep = Annotated["JobStore", Depends(get_job_store)]
ResolverDep = Annotated["TrackResolver", Depends(get_resolver)]
SettingsDep = Annotated["Settings", Depends(get_settings_dependency)]
UserContextDep = Annotated[UserContext, Depends(get_user_context)]
