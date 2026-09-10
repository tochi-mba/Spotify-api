"""Typed models for Spotify's own objects.

One model per response, so the OpenAPI schema describes what actually comes
back and a client can be generated from it. Every field Spotify documents as
optional is optional here, and unknown fields are ignored rather than rejected
-- Spotify adds fields without warning, and a new one must never turn a working
call into a 500.
"""

from __future__ import annotations
