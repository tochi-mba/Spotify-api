"""The Spotify adapter.

Everything that knows Spotify exists lives behind this package. The rest of the
service talks to the :class:`~spotify_api.spotify.protocols.TrackResolver`
protocol and never sees an HTTP detail.
"""

from __future__ import annotations
