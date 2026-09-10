"""The concrete implementations really do satisfy the seams routes depend on."""

from __future__ import annotations

import httpx

from spotify_api.credentials.keyring import KeyringCredentialProvider
from spotify_api.credentials.protocols import CredentialProvider
from spotify_api.spotify.client import SpotifyClient
from spotify_api.spotify.protocols import TrackResolver
from spotify_api.spotify.resolver import SpotifyTrackResolver
from tests.factories import make_settings


def test_the_keyring_provider_satisfies_the_credential_seam() -> None:
    provider = KeyringCredentialProvider(client=httpx.AsyncClient(), settings=make_settings())
    assert isinstance(provider, CredentialProvider)


def test_the_resolver_satisfies_the_seam_routes_depend_on() -> None:
    settings = make_settings()
    credentials = KeyringCredentialProvider(client=httpx.AsyncClient(), settings=settings)
    client = SpotifyClient(client=httpx.AsyncClient(), credentials=credentials, settings=settings)
    assert isinstance(SpotifyTrackResolver(client=client, settings=settings), TrackResolver)
