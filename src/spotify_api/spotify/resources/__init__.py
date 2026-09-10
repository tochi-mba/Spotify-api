"""One module per Spotify resource group.

Each is a thin set of callers over ``SpotifyClient.request``, so the retry
ladder, credential handling and error translation are written once and every
resource inherits them.
"""

from __future__ import annotations
