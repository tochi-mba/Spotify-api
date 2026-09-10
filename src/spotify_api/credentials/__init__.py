"""Credential resolution.

This service stores no Spotify credential of any kind. For every request it
asks keyring -- a separate credentials service -- what to attach, and attaches
it. keyring owns the OAuth grant, refreshes it, and never hands back the
underlying token.
"""

from __future__ import annotations
