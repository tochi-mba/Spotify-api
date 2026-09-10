# Architecture

## Layers

Three layers, each depending only on the one below, with the Spotify adapter
reachable only through a protocol.

```
┌──────────────────────────────────────────────────────────────┐
│ HTTP boundary            api/routes/, middleware.py,         │
│                          errors.py handlers                  │
│   • validates, renders, correlates. Knows nothing of Spotify.│
├──────────────────────────────────────────────────────────────┤
│ Contract                 models/requests.py, responses.py    │
│   • the published schema; every caller rule lives here       │
├──────────────────────────────────────────────────────────────┤
│ Seam                     spotify/protocols.py                │
│   • TrackResolver: the only thing routes are allowed to know │
├──────────────────────────────────────────────────────────────┤
│ Adapter                  spotify/{resolver,client,auth,      │
│                                   query,mappers}.py          │
│   • the only code that imports httpx or understands Spotify  │
└──────────────────────────────────────────────────────────────┘
```

The seam is the load-bearing part. Routes take a `TrackResolver`, so:

- route tests inject a fake and stay hermetic and fast;
- adding albums or artists later is a new implementation, not a rewrite;
- Spotify's vocabulary cannot leak into the transport layer by accident.

## Request lifecycle

`POST /v1/lookup` with two items:

```
1. RequestContextMiddleware
     accepts or generates X-Request-ID, binds it to a ContextVar and stashes it
     on request.state, starts the timer
2. FastAPI + Pydantic
     validate the body against LookupRequest. A violation raises here and never
     reaches the handler → 422 with field-level detail
3. lookup handler
     checks the operational batch cap (configurable, so not in the schema)
     resolves the TrackResolver from app.state via dependency injection
4. SpotifyTrackResolver.resolve
     opens a semaphore of max_concurrency and gathers one task per item
5. per item → SpotifyClient.search_track
     builds a field-filtered query, asks ClientCredentialsProvider for a token
     (cached; a cold cache collapses concurrent callers into one fetch),
     issues GET /search, applies the retry ladder
6. mappers.to_track
     raw JSON → Track, defensively; anything unreadable becomes None
7. resolver
     each item becomes found / not_found / error. A failure is caught here, so
     it costs that item and nothing else
8. handler → LookupResponse, count computed from the results
9. middleware
     echoes X-Request-ID, logs method, path, status and duration, unbinds
```

## Why one HTTP client

Created once in the app lifespan, shared by the token provider and the search
client, closed on shutdown. A client per request would throw away connection
pooling and TLS session reuse, which is most of the cost of talking to Spotify.
`tests/integration/test_app.py` asserts the graph really does share one.

## Why liveness and readiness are separate

`/healthy` answers from process state alone. `/ready` obtains a token. If a
single endpoint did both, Spotify having a bad afternoon would look like a
crash loop, and an orchestrator would restart containers that were fine.

## Failure handling

| Where | What happens |
|---|---|
| Schema violation | 422 before the handler runs, with pydantic's detail |
| Batch over the operational cap | 422 from the handler |
| Spotify 401 | one forced token refresh, then retried; a repeat is a credential error |
| Spotify 403 | fails immediately — retrying cannot help |
| Spotify 429 | honours `Retry-After`, else exponential backoff, within budget |
| Spotify 5xx / timeout / reset | exponential backoff with a cap, within budget |
| Budget exhausted | the item becomes an `error` result; the batch still returns 200 |
| Anything unanticipated | opaque `internal_error`; detail goes to the logs only |

## Concurrency

- **Within a batch:** `asyncio.Semaphore(MAX_CONCURRENCY)`. Fifty simultaneous
  searches would earn an immediate 429; eight is fast and well inside tolerance.
- **Token acquisition:** an `asyncio.Lock` with a re-check after acquiring, so a
  burst arriving on a cold cache triggers one fetch. Twenty concurrent callers
  are tested to produce exactly one token request.
- **Ordering:** `asyncio.gather` preserves input order regardless of completion
  order, which is what lets callers line results up with what they submitted.

## Observability

Every log line is one JSON object carrying `request_id`, so a batch is traceable
across route, resolver, client and auth. The id is stamped onto each `LogRecord`
at creation rather than read at format time — it belongs to the record, not to
whoever renders it.
