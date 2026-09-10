# Spotify Lookup API

An HTTP service that resolves batches of loosely-specified tracks against the
[Spotify Web API](https://developer.spotify.com/documentation/web-api).

Submit an array of `{ name, artist?, album?, year? }` and get back one fully
populated result per item — in order, each with its own status. Only `name` is
required; the rest are hints that narrow the search.

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![Coverage 100%](https://img.shields.io/badge/coverage-100%25-brightgreen)](#testing)

---

## Why it exists

Music metadata arrives messy. You have a track title, maybe an artist, maybe a
year — scraped from a spreadsheet, a playlist export, or a user typing. This
service turns that into canonical Spotify data: IDs, URIs, ISRCs, album art
context, duration, popularity.

It is designed to sit behind something else — an MCP server, a batch job, a
frontend — so the contract is deliberately boring and stable, and the OpenAPI
schema is complete enough to generate a client from.

## Quickstart

```bash
git clone https://github.com/tochi-mba/Spotify-api.git
cd Spotify-api
uv sync --all-extras

cp .env.example .env      # then add your credentials (see below)
uv run python -m spotify_api
```

Interactive docs at <http://localhost:8000/docs>.

### Getting Spotify credentials

1. Sign in at the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard).
2. **Create app** — any name and description; the redirect URI is unused by this
   service, so `http://localhost:8000/callback` is fine.
3. Copy the **Client ID** and **Client Secret** into your `.env`.

This service uses the Client Credentials flow, which reaches only public
catalogue data. It never asks for a user login and can never touch anyone's
account, playlists or listening history.

---

## API

### `POST /v1/lookup`

```bash
curl -s -X POST localhost:8000/v1/lookup \
  -H 'content-type: application/json' \
  -d '{
    "items": [
      { "name": "Bohemian Rhapsody", "artist": "Queen", "year": 1975 },
      { "name": "Redbone" },
      { "name": "a track that does not exist" }
    ],
    "market": "GB"
  }' | jq
```

**Request**

| Field | Type | Required | Notes |
|---|---|---|---|
| `items` | array | yes | 1–50 items (configurable). Results come back in this order. |
| `items[].name` | string | yes | Track title, 1–200 chars. |
| `items[].artist` | string | no | Strongly recommended — disambiguates common titles. |
| `items[].album` | string | no | |
| `items[].year` | integer | no | 1900 – next year. |
| `market` | string | no | ISO 3166-1 alpha-2, e.g. `GB`. Affects availability. |

Unknown fields are **rejected**, not ignored — a misspelled key is an error you
want to hear about.

**Response** — always one result per submitted item:

```json
{
  "request_id": "0f8c...",
  "count": 3,
  "results": [
    {
      "index": 0,
      "query": { "name": "Bohemian Rhapsody", "artist": "Queen", "album": null, "year": 1975 },
      "status": "found",
      "track": {
        "id": "7tFiyTwD0nx5a1eklYtX2J",
        "name": "Bohemian Rhapsody",
        "artists": [{ "id": "1dfeR4HaWDbWqFHLkxsg1d", "name": "Queen" }],
        "album": {
          "id": "1GbtB4zTqAsyfZEsm1RZfx",
          "name": "A Night At The Opera (2011 Remaster)",
          "release_date": "1975-11-21",
          "release_year": 1975
        },
        "duration_ms": 354320,
        "explicit": false,
        "popularity": 82,
        "isrc": "GBUM71029604",
        "preview_url": null,
        "external_url": "https://open.spotify.com/track/7tFiyTwD0nx5a1eklYtX2J",
        "uri": "spotify:track:7tFiyTwD0nx5a1eklYtX2J"
      },
      "error": null
    },
    { "index": 1, "query": { "name": "Redbone", "artist": null, "album": null, "year": null },
      "status": "found", "track": { "...": "..." }, "error": null },
    { "index": 2, "query": { "name": "a track that does not exist", "artist": null, "album": null, "year": null },
      "status": "not_found", "track": null, "error": null }
  ]
}
```

**Partial success is the point.** `status` is `found`, `not_found` or `error`
per item. One track that cannot be resolved returns an `error` result and costs
you nothing else — the request is still `200`.

### `GET /healthy` — liveness

```json
{ "status": "ok", "service": "spotify-api", "version": "0.1.0", "uptime_seconds": 12.34 }
```

Makes no outbound call, so it can never be failed by a third party. Always `200`
while the process is serving. Use it for a liveness probe.

### `GET /ready` — readiness

`200` when a Spotify token can be obtained, `503` when it cannot:

```json
{ "status": "ready", "dependencies": { "spotify": "ok" } }
```

Use it to gate traffic. The split matters: without it, an orchestrator would
restart a perfectly healthy container because Spotify was having a bad
afternoon.

### Errors

Every non-2xx response shares one envelope:

```json
{
  "error": {
    "type": "validation_error",
    "message": "the request body is invalid",
    "details": { "errors": [] },
    "request_id": "0f8c..."
  }
}
```

| `type` | Status | Meaning |
|---|---|---|
| `validation_error` | 422 | The body violates the schema. `details.errors` has the field-level reasons. |
| `batch_too_large` | 422 | More items than this deployment allows. |
| `spotify_auth_error` | 503 | Spotify rejected our credentials. |
| `spotify_rate_limit_error` | 503 | Rate limited past the retry budget. |
| `spotify_unavailable_error` | 503 | Spotify unreachable or failing. |
| `internal_error` | 500 | Unanticipated. Detail is in the logs, never the response. |

Pass `X-Request-ID` and it is echoed back on the response and stamped on every
log line the request produced. Omit it and one is generated.

---

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `SPOTIFY_CLIENT_ID` | — | **Required.** |
| `SPOTIFY_CLIENT_SECRET` | — | **Required.** |
| `SPOTIFY_API_BASE_URL` | `https://api.spotify.com/v1` | Override for sandboxes. |
| `SPOTIFY_ACCOUNTS_BASE_URL` | `https://accounts.spotify.com` | Token issuance. |
| `REQUEST_TIMEOUT_SECONDS` | `10.0` | Per upstream request. |
| `MAX_RETRIES` | `3` | Retries for 429/5xx/transport errors. |
| `RETRY_BACKOFF_BASE_SECONDS` | `0.2` | Exponential, capped at 8s. |
| `MAX_CONCURRENCY` | `8` | Simultaneous searches per batch. |
| `MAX_BATCH_SIZE` | `50` | Operational cap on `items`. |
| `TOKEN_EXPIRY_SKEW_SECONDS` | `60` | Refresh this long before expiry. |
| `DEFAULT_MARKET` | unset | Applied when a request omits `market`. |
| `ENVIRONMENT` | `development` | `development`/`test`/`staging`/`production`. |
| `LOG_LEVEL` | `INFO` | |
| `LOG_FORMAT` | `json` | `json` or `console`. |

Missing credentials are a **startup failure**, by design — better than running
misconfigured.

## Design notes

- **One shared HTTP client.** Created in the app lifespan, closed on shutdown.
  A client per request would discard connection pooling and TLS session reuse,
  which is most of the cost of talking to Spotify.
- **Token caching with a stampede lock.** A cold cache hit by a fifty-item batch
  triggers exactly one token fetch, not fifty.
- **Field-filtered search.** `track:"Bohemian Rhapsody" artist:"Queen"` rather
  than a bag of words, which also matches tribute covers.
- **Bounded concurrency.** Fifty simultaneous searches would earn an immediate
  429; a semaphore keeps us inside Spotify's tolerance while staying far faster
  than serial resolution.
- **Spotify stays behind a seam.** Routes depend on a `TrackResolver` protocol,
  so nothing outside `spotify/` knows Spotify exists.

## Testing

```bash
make test        # full suite, 100% branch coverage enforced
make check       # ruff + format + mypy --strict + tests
uv run pytest -m live   # real Spotify API; needs credentials, skips without
```

Coverage is **100%, branch-inclusive, enforced in CI**. Tests are hermetic:
Spotify is faked at the HTTP transport, so the suite needs no network and no
credentials. See [docs/testing.md](docs/testing.md).

## Docker

```bash
docker build -t spotify-api .
docker run --rm -p 8000:8000 --env-file .env spotify-api
```

## Documentation

- [AGENTS.md](AGENTS.md) — operating manual for contributors and agents
- [docs/architecture.md](docs/architecture.md) — layers and request lifecycle
- [docs/api.md](docs/api.md) — full endpoint reference
- [docs/testing.md](docs/testing.md) — test taxonomy and fixtures
- [docs/adr/](docs/adr/) — architecture decision records
- [CONTRIBUTING.md](CONTRIBUTING.md)

## Licence

MIT — see [LICENSE](LICENSE).
