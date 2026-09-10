"""The concrete implementations really do satisfy the seams routes depend on."""

from __future__ import annotations

import httpx

from spotify_api.spotify.auth import ClientCredentialsProvider
from spotify_api.spotify.client import SpotifyClient
from spotify_api.spotify.protocols import TokenProvider, TrackResolver
from spotify_api.spotify.resolver import SpotifyTrackResolver
from tests.factories import make_settings


def test_the_credentials_provider_satisfies_the_token_provider_protocol() -> None:
    provider = ClientCredentialsProvider(client=httpx.AsyncClient(), settings=make_settings())
    assert isinstance(provider, TokenProvider)


def test_the_spotify_client_satisfies_the_token_provider_it_is_given() -> None:
    settings = make_settings()
    provider = ClientCredentialsProvider(client=httpx.AsyncClient(), settings=settings)
    client = SpotifyClient(client=httpx.AsyncClient(), token_provider=provider, settings=settings)
    # The resolver is what routes actually depend on; it must satisfy the seam.
    resolver = SpotifyTrackResolver(client=client, settings=settings)
    assert isinstance(resolver, TrackResolver)
