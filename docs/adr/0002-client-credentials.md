# ADR 0002 — Authenticate with the Client Credentials flow

**Status:** accepted

## Context

Spotify offers Authorization Code (acting as a user) and Client Credentials
(acting as the application). This service reads public catalogue metadata only.

## Decision

Client Credentials, with an in-memory token cache guarded by an `asyncio.Lock`.

## Rationale

- It needs no user login, no redirect URI, no refresh-token storage, and no
  consent screen. The deployment is two environment variables.
- It cannot reach any user's account, playlists or listening history — not
  merely by policy but because the grant has no user context. That is a
  meaningful security property: a compromise of this service exposes nothing
  personal.
- Tokens last an hour, so caching turns per-lookup auth traffic into roughly one
  request per hour.

## Consequences

- Endpoints requiring user context (personalisation, playlist writes) are out of
  reach. If they are ever needed, that is a separate provider and a separate
  ADR, not an extension of this one.
- The cache must be stampede-safe: a cold cache hit by a fifty-item batch would
  otherwise fire fifty simultaneous token requests. A lock with a re-check after
  acquisition collapses those into one; a test asserts twenty concurrent callers
  produce exactly one fetch.
- The token is process-local. Multiple replicas each hold their own, which is
  fine at this scale and avoids a shared cache dependency.
- Time is injected as a `clock` callable so expiry logic is tested directly
  rather than waited for.
