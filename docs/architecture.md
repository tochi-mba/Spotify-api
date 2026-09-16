# Architecture

## The shape

The HTTP boundary knows nothing of Spotify. Routes depend on protocols; the adapter behind
those protocols is the only code that understands Spotify's JSON, URLs, or retry ladder.
Identity is delegated to keyring. Direction is enforced by import-linter contracts in
`pyproject.toml`, so a violation fails CI rather than being noticed in review.

```
                       ┌──────────────────────────────────┐
   HTTP request ──────▶│  api/   Bearer verification,     │
   (bearer token)      │         routers, problem+json,   │
                       │         request ids, ?async=     │
                       └──────────────┬───────────────────┘
                                      │
              ┌───────────────────────┼───────────────────────┐
              │                       │                       │
   ┌──────────▼──────────┐  ┌─────────▼─────────┐  ┌─────────▼──────────┐
   │ jobs/  store +      │  │ models/  public   │  │ credentials/       │
   │        runner +     │  │          contract │  │  keyring headers,  │
   │        confirm      │  │                   │  │  cached to expiry  │
   └──────────┬──────────┘  └───────────────────┘  └─────────┬──────────┘
              │                                              │
   ┌──────────▼──────────┐                        ┌──────────▼──────────┐
   │ spotify/  client,   │◀── TrackResolver ──────│ keyring (external)  │
   │           resolver, │                        │  JWKS + credentials │
   │           player    │                        └─────────────────────┘
   └──────────┬──────────┘
              │
              ▼
         Spotify Web API
```

`config.py`, `logging.py`, `context.py` and `errors.py` sit beside these packages as a shared
kernel. `app.py` is the composition root: it owns the one `httpx.AsyncClient`, builds the
JWKS client and token verifier, and wires the graph onto `app.state`.

What the contracts actually refuse:

- `models/` and `jobs/` must not import `api/`.
- `keyring_client` is imported only from config validation, the app factory, the auth
  dependency, and the credentials package.
- `httpx` stays out of `api/` and `models/`. The composition root, the credentials adapter and
  the Spotify client are the three places that open sockets.

There is no in-process Client Credentials grant. Spotify access belongs to a person, lives in
keyring, and is resolved per request.

## Who is asking

```
person ──login──▶ keyring ──session──▶ their assistant
                     │
  assistant asks keyring for a token with audience "spotify-api"
                     │
  assistant ──Authorization: Bearer <token>──▶ spotify-api
                                                  │
                        verified locally against keyring's JWKS (no round trip)
                                                  │
                        needs Spotify headers:
                        ├─ Authorization: Bearer <this service's own service token>
                        └─ X-Keyring-User-Token: <the same token it was handed>
                                                  │
                                                  ▼
                                               keyring
```

Three properties hold this together:

1. **This service mints nothing.** The account is the verified `sub` and nothing else.
2. **Verification is local.** Keys are cached; the price is that a revoked session stays valid
   until its token expires.
3. **Stored state is scoped by that account.** A job belongs to whoever started it. Another
   account's job is `404`, never `403`.

## Request lifecycle

`POST /v1/lookup` with two items:

```
1. RequestContextMiddleware
     accepts or generates X-Request-ID, binds it to a ContextVar, starts the timer
2. FastAPI + Pydantic
     validate the body against LookupRequest. A violation raises here and never
     reaches the handler → 422 problem+json with field-level errors (never the input)
3. get_user_context
     parses Authorization: Bearer (or the legacy X-Keyring-User-Token), verifies the
     token locally, binds the account id into the request context
4. lookup handler
     checks the operational batch cap (configurable, so not in the schema)
     resolves the TrackResolver from app.state
5. SpotifyTrackResolver.resolve
     opens a semaphore of max_concurrency and gathers one task per item
6. per item → SpotifyClient.search_track
     asks KeyringCredentialProvider for headers (cached; a cold cache collapses
     concurrent callers into one fetch), issues GET /search, applies the retry ladder
7. mappers.to_track
     raw JSON → Track, defensively; anything unreadable becomes None
8. resolver
     each item becomes found / not_found / error. A failure is caught here, so
     it costs that item and nothing else. A refused token or a missing Spotify
     connection fails the whole batch instead.
9. handler → LookupResponse, count computed from the results
10. middleware
     echoes X-Request-ID and X-Response-Time-Ms, logs method, path, status and
     duration, unbinds
```

`?async=true` submits the same coroutine to `JobRunner` and answers `202`. For playback the
job is not terminal until `PlaybackConfirmer` has seen the effect on the device.

## Why one HTTP client

Created once in the app lifespan, shared by the credential provider and the Spotify client,
closed on shutdown. A client per request would throw away connection pooling and TLS session
reuse, which is most of the cost of talking to Spotify or keyring. httpx pools per host, so
one client serving two upstreams is not a compromise. `tests/integration/test_app.py` asserts
the graph really does share one.

The JWKS client is a separate object, built with the app rather than in the lifespan, so a
request is verifiable before the lifespan has run. Constructing it makes no network call.

## Why liveness and readiness are separate

`/healthy` answers from process state alone. `/ready` fetches keyring's published key
document. If a single endpoint did both, keyring having a bad afternoon would look like a
crash loop, and an orchestrator would restart containers that were fine.

`/ready` does not probe keyring's own `/healthy`: that endpoint answers `503` whenever any one
stored connection has stopped working, so one expired grant would take this service out of
rotation for everybody. The keys are what this process needs before it can serve anybody.

## Failure handling

| Where | What happens |
| --- | --- |
| Schema violation | 422 problem before the handler runs, location and message only |
| Batch over the operational cap | 422 from the handler, `details.limit` set |
| Missing or refused token | 401, one fixed message, `WWW-Authenticate: Bearer` |
| keyring keys unreachable | 503 — the token may be fine |
| No Spotify connection on this profile | 502 — retrying cannot help |
| Spotify 401 | one forced credential refresh, then retried; a repeat is an auth error |
| Spotify 403 Premium | 403 `premium_required` |
| Spotify 404 on a player command | 409 `no_active_device` when that is what it means |
| Spotify 429 | honours `Retry-After`, else exponential backoff, within budget |
| Spotify 5xx / timeout / reset | exponential backoff with a cap, within budget |
| Budget exhausted | the item becomes an `error` result; the batch still returns 200 |
| Command accepted, effect never seen | 504, last player state in `details.observed` |
| Anything unanticipated | opaque 500; type and stack in the logs, never the exception's text |

## Concurrency

- **Within a batch:** `asyncio.Semaphore(MAX_CONCURRENCY)`. Fifty simultaneous searches would
  earn an immediate 429; eight is fast and well inside tolerance.
- **Credential acquisition:** an `asyncio.Lock` per cache key with a re-check after acquiring,
  so a burst arriving on a cold cache triggers one fetch.
- **Ordering:** `asyncio.gather` preserves input order regardless of completion order, which
  is what lets callers line results up with what they submitted.
- **Jobs:** each belongs to one account; the store takes `account_id` as a parameter, never
  from a context variable. Cross-account access is `404`.

## Observability

Every log line is one JSON object (or a console line locally) produced by structlog. The
request id is bound at the edge; the verified account is bound once the token has been
checked. A redaction pass runs last before rendering and replaces every secret-looking field,
at every depth. An exception is rendered as its stack and its type name — never its message,
because an HTTP client's message carries the URL it was given, and a URL can carry
credentials.

Module-level loggers resolve against the configuration in force *when they are called*, not
when the module was imported. Binding eagerly at import time would freeze every logger to
structlog's defaults, and `SPOTIFY_API_LOG_FORMAT=json` would validate and do nothing.
