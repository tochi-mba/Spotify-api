"""Dependency wiring.

Routes ask for a :class:`TrackResolver`, never for a Spotify client. The
concrete graph is assembled once during application startup and stashed on
``app.state``; these providers hand it out. Tests replace either provider
through FastAPI's ``dependency_overrides``.

:func:`get_user_context` is the one that matters most: it is the only way a request becomes an
account in this service, and the account is the verified ``sub`` of the caller's token and
nothing else.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

# Auth dependency: the only request-path import of keyring_client. Verification is local.
from keyring_client import AuthenticationError, ExactAudience, KeyringUnreachableError
from pydantic import SecretStr

from spotify_api.context import set_account_id
from spotify_api.credentials.models import UserContext
from spotify_api.errors import KeyringUnavailableError, UserTokenRejectedError
from spotify_api.logging import get_logger
from spotify_api.preferences import Preferences

if TYPE_CHECKING:
    from keyring_client import TokenVerifier

    from spotify_api.config import Settings
    from spotify_api.jobs.confirm import PlaybackConfirmer
    from spotify_api.jobs.protocols import JobStore
    from spotify_api.jobs.runner import JobRunner
    from spotify_api.preferences import PreferenceSource
    from spotify_api.spotify.protocols import TrackResolver
    from spotify_api.spotify.resources.player import PlayerResource

__all__ = [
    "AUTHORIZATION_HEADER",
    "PROFILE_HEADER",
    "USER_TOKEN_HEADER",
    "ConfirmerDep",
    "JobRunnerDep",
    "JobStoreDep",
    "PlayerDep",
    "PreferencesDep",
    "ResolverDep",
    "SettingsDep",
    "UserContextDep",
    "bearer_scheme",
    "get_confirmer",
    "get_job_runner",
    "get_job_store",
    "get_player",
    "get_preferences",
    "get_resolver",
    "get_settings_dependency",
    "get_user_context",
]

_logger = get_logger(__name__)

#: Where the caller's keyring token travels: ``Authorization: Bearer <token>``.
AUTHORIZATION_HEADER = "Authorization"

#: Where the token used to travel. Accepted for one more release, and logged whenever it is used.
USER_TOKEN_HEADER = "X-Keyring-User-Token"  # noqa: S105 -- a header name, not a credential

#: Which keyring profile to read the Spotify connection from.
PROFILE_HEADER = "X-Keyring-Profile"

#: Said for every token this service refuses, whichever rule refused it.
TOKEN_NOT_ACCEPTED = "the keyring user token was not accepted"  # noqa: S105 -- a message

#: Said when no token was presented at all. Naming the header tells the caller nothing they lack.
MISSING_CREDENTIALS = (
    "a keyring user token is required as 'Authorization: Bearer <token>'; mint one from keyring "
    "with this service as the audience"
)

#: Said when keyring's published keys cannot be fetched, so no token can be checked.
KEYS_UNAVAILABLE = "keyring's signing keys could not be fetched; try again shortly"

bearer_scheme = HTTPBearer(
    auto_error=False,
    description=(
        "A short-lived RS256 token from keyring, minted for this service with "
        '`POST /v1/auth/service-token {"audience": "spotify-api"}`. Required on every `/v1` '
        "route. The `X-Keyring-User-Token` header is still accepted for one release."
    ),
)
"""``auto_error=False`` so a missing or malformed header raises our error, in our problem shape.

Left to itself, HTTPBearer answers with FastAPI's own error body -- a different shape from every
other failure this service produces, on the single most common mistake a caller can make. It is
also what publishes the scheme in the OpenAPI document, which a header parameter named
``Authorization`` cannot do: OpenAPI ignores those.
"""


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


def get_preference_source(request: Request) -> PreferenceSource:
    """Return the per-person settings source assembled at startup."""
    source: PreferenceSource = request.app.state.preferences
    return source


async def get_user_context(
    request: Request,
    bearer: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    x_keyring_user_token: Annotated[
        str | None,
        Header(
            alias=USER_TOKEN_HEADER,
            deprecated=True,
            description=(
                "Deprecated: send the token as `Authorization: Bearer <token>` instead. Still "
                "accepted for one release; when both headers are sent they must carry the "
                "same token."
            ),
        ),
    ] = None,
    x_keyring_profile: Annotated[
        str | None,
        Header(
            alias=PROFILE_HEADER,
            description="Which keyring profile to use. Defaults to the person's default profile.",
        ),
    ] = None,
) -> UserContext:
    """Verify the caller's token and build the acting-user context from it.

    The token is checked here, locally, against keyring's published keys -- signature,
    algorithm, issuer, audience and expiry -- before any work starts. The account id on the
    context is the verified ``sub`` and nothing else, so everything this service stores is
    scoped by an identity nobody can simply claim. The account is also bound into the request
    context, so every log record the rest of the request produces says whose request it was.

    Raises:
        UserTokenRejectedError: no token, a malformed or contradictory pair of headers, or a
            token keyring did not mint for this service. One message for every refusal of a
            presented token, so a forger learns nothing from the difference.
        KeyringUnavailableError: keyring's keys could not be fetched, so the token could not
            be checked either way. A 503, because the token may be perfectly good.
    """
    token = _presented_token(request, bearer=bearer, legacy=x_keyring_user_token)

    settings: Settings = request.app.state.settings
    verifier: TokenVerifier = request.app.state.verifier
    try:
        verified = await verifier.verify(token, audience=ExactAudience(settings.keyring_audience))
    except AuthenticationError as exc:
        raise UserTokenRejectedError(TOKEN_NOT_ACCEPTED) from exc
    except KeyringUnreachableError as exc:
        raise KeyringUnavailableError(KEYS_UNAVAILABLE) from exc

    set_account_id(verified.account_id)
    source = get_preference_source(request)
    preferences = await source.for_token(token)
    request.state.preferences = preferences
    requested_profile = (x_keyring_profile or "").strip() or None
    profile = preferences.profile(requested_profile)
    return UserContext(account_id=verified.account_id, user_token=SecretStr(token), profile=profile)


async def get_preferences(
    request: Request, _context: Annotated[UserContext, Depends(get_user_context)]
) -> Preferences:
    """Return the preferences resolved for this request.

    Depends on :func:`get_user_context` so the source is asked once, after the token
    has been verified, and the values are the ones already used to pick a profile.
    """
    preferences: Preferences = request.state.preferences
    return preferences


def _presented_token(
    request: Request, *, bearer: HTTPAuthorizationCredentials | None, legacy: str | None
) -> str:
    """The token the caller presented, by the rule every service in the family uses.

    An ``Authorization`` header that is present must be a Bearer credential, and when the legacy
    header is sent as well the two must carry the same token; anything else is refused in the
    words every other bad token gets. The legacy header alone is accepted, and logged, so the
    callers still sending it can be found before it stops working.

    ``bearer`` is what :data:`bearer_scheme` parsed, which is ``None`` both when there is no
    ``Authorization`` header and when there is one that is not a Bearer credential. The raw
    header tells those apart: the first may fall back to the legacy header, the second may not.

    Raises:
        UserTokenRejectedError: see :func:`get_user_context`.
    """
    token = legacy
    if request.headers.get(AUTHORIZATION_HEADER) is not None:
        if bearer is None or (token and token != bearer.credentials):
            raise UserTokenRejectedError(TOKEN_NOT_ACCEPTED)
        token = bearer.credentials
    elif token:
        _logger.info("legacy_user_token_header", replacement="Authorization: Bearer")
    if not token:
        raise UserTokenRejectedError(MISSING_CREDENTIALS)
    return token


ConfirmerDep = Annotated["PlaybackConfirmer", Depends(get_confirmer)]
JobRunnerDep = Annotated["JobRunner", Depends(get_job_runner)]
PlayerDep = Annotated["PlayerResource", Depends(get_player)]
JobStoreDep = Annotated["JobStore", Depends(get_job_store)]
PreferencesDep = Annotated[Preferences, Depends(get_preferences)]
ResolverDep = Annotated["TrackResolver", Depends(get_resolver)]
SettingsDep = Annotated["Settings", Depends(get_settings_dependency)]
UserContextDep = Annotated[UserContext, Depends(get_user_context)]
