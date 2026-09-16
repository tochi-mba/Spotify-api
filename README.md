# Spotify Lookup API

An HTTP service that resolves batches of loosely-specified tracks against the
[Spotify Web API](https://developer.spotify.com/documentation/web-api), and controls
playback with every command confirmed on the device.

Submit an array of `{ name, artist?, album?, year? }` and get back one fully
populated result per item — in order, each with its own status. Only `name` is
required; the rest are hints that narrow the search.

Part of the LUCY assistant family. It holds **no Spotify credential of its own**:
every request carries the caller's keyring token, this service verifies it
locally, and [keyring](https://github.com/tochi-mba/Keyring-api) hands over the
headers to attach for that person's own Spotify connection.

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
# Clone this repository next to Keyring-api: keyring-client is installed from
# ../Keyring-api/clients/python until it is published.
git clone https://github.com/tochi-mba/Spotify-api.git
cd Spotify-api
make install

cp .env.example .env      # point it at keyring (see Configuration)
make run                  # http://localhost:8007/docs
```

### Connecting a Spotify account

Spotify access belongs to a person and lives in keyring, never here.

1. **Register this service with keyring.** Add `"spotify-api": "<32+ random characters>"`
   to keyring's `KEYRING_SERVICE_TOKENS`, and put the same value in
   `SPOTIFY_API_KEYRING_SERVICE_TOKEN`.
2. **Connect Spotify to a profile.** With keyring's Spotify OAuth provider configured,
   each person runs `POST /v1/profiles/{profile}/connections/spotify/authorize` in keyring
   and completes the consent screen.
3. **Call this service with a token minted for it.** A caller mints a short-lived token
   with `POST /v1/auth/service-token {"audience": "spotify-api"}` and sends it as
   `Authorization: Bearer <token>`. `X-Keyring-User-Token` is still accepted for one release.
   `X-Keyring-Profile` chooses the profile, defaulting to
   `SPOTIFY_API_KEYRING_DEFAULT_PROFILE`.

---

## API

Every `/v1` route requires `Authorization: Bearer <token>`. It is verified locally against
keyring's published keys before any work starts: RS256 only, issuer and audience
(`spotify-api`) pinned, expiry checked. A token keyring did not mint for this
service is a `401` with one fixed message whichever rule refused it; keyring's
keys being unreachable is a `503`, because the token may be perfectly good.

| Route | Purpose |
|---|---|
| `POST /v1/lookup` | Resolve a batch of tracks, with partial success. |
| `GET /v1/player`, `/devices`, `/currently-playing`, `/queue`, `/recently-played` | Read playback state. |
| `POST /v1/player/play`, `/pause`, `/next`, `/previous`, `/seek`, `/volume`, `/shuffle`, `/repeat`, `/transfer`, `/queue` | Issue a command and answer with the player state that confirms it took effect. |
| `GET /v1/jobs`, `GET /v1/jobs/{job_id}`, `DELETE /v1/jobs/{job_id}` | Poll or cancel work started with `?async=true`. |
| `GET /healthy`, `GET /ready` | Liveness and readiness. No token needed. |

Interactive docs at `/docs`, the schema at `/openapi.json`, and the full reference
in [docs/api.md](docs/api.md).

### `POST /v1/lookup`

```bash
curl -s -X POST localhost:8007/v1/lookup \
  -H "Authorization: Bearer $TOKEN" \
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

### Background jobs

Every route takes `?async=true`, which answers `202` with a `job_id` and a
`poll_url` instead of holding the connection open. A job belongs to the account
that started it: only that account can list, read or cancel it, and anybody else
gets the same `404` an unknown job gets. Jobs are held in memory, so they are lost
on restart and are not shared between replicas.

### `GET /healthy` — liveness

```json
{ "status": "ok", "service": "spotify-api", "version": "0.1.0", "uptime_seconds": 12.34 }
```

Makes no outbound call, so it can never be failed by a third party. Always `200`
while the process is serving. Use it for a liveness probe.

### `GET /ready` — readiness

`200` when keyring's published key document can be fetched, `503` when it cannot:

```json
{ "status": "ready", "dependencies": { "spotify": "ok" } }
```

It deliberately does not probe keyring's own `/healthy`, which answers `503`
whenever any single person's stored connection has stopped working — one expired
grant would otherwise take this service out of rotation for everybody.

### Errors

Every non-2xx response is RFC 9457 `application/problem+json`:

```json
{
  "type": "https://spotify-api.invalid/problems/validation-failed",
  "title": "Validation failed",
  "status": 422,
  "detail": "the request failed validation",
  "instance": "/v1/lookup",
  "request_id": "0f8c...",
  "errors": [{"location": "body.items.0.name", "message": "String should have at least 1 character"}]
}
```

Switch on the last segment of `type`. `request_id` is on every problem, including a 500.

| Last segment of `type` | Status | Meaning |
|---|---|---|
| `user-token-rejected` | 401 | No token, or one this service will not accept. |
| `premium-required` | 403 | Playback control needs Spotify Premium. |
| `job-not-found` | 404 | No such job for your account, or it has aged out. |
| `no-active-device` | 409 | Nothing to play on. Open Spotify on a device, or pass `device_id`. |
| `validation-failed` | 422 | The body violates the schema. `errors` has the field-level reasons. |
| `batch-too-large` | 422 | More items than this deployment allows. |
| `credential-unavailable` | 502 | You have not connected Spotify on that profile, or the grant stopped working. Reconnect it in keyring. |
| `keyring-unavailable` | 503 | keyring, or its published keys, could not be reached. |
| `spotify-auth-error` | 503 | Spotify refused the credential keyring provided. |
| `spotify-rate-limit-error` | 503 | Rate limited past the retry budget. |
| `spotify-unavailable-error` | 503 | Spotify unreachable or failing. |
| `confirmation-timeout` | 504 | Spotify accepted a command but its effect never became observable. |
| `internal-server-error` | 500 | Unanticipated. Detail is in the logs, never the response. |

Pass `X-Request-ID` and it is echoed back on the response and stamped on every
log line the request produced. Omit it and one is generated.

---

## Configuration

Every variable is prefixed `SPOTIFY_API_`. An unrecognised `SPOTIFY_API_*`
variable is a startup error, and so is any of the names this service read before
it had a prefix — the error names the variable that replaced it.
[.env.example](.env.example) lists every setting with its default.

| Variable | Default | Notes |
|---|---|---|
| `SPOTIFY_API_KEYRING_BASE_URL` | — | **Required.** Where keyring is. |
| `SPOTIFY_API_KEYRING_SERVICE_TOKEN` | — | **Required.** This service's entry in keyring's `KEYRING_SERVICE_TOKENS`; at least 32 characters. |
| `SPOTIFY_API_KEYRING_ISSUER` | `http://127.0.0.1:8001` | Must equal keyring's `KEYRING_ISSUER`. |
| `SPOTIFY_API_KEYRING_AUDIENCE` | `spotify-api` | Must equal this service's name in `KEYRING_SERVICE_TOKENS`. |
| `SPOTIFY_API_KEYRING_DEFAULT_PROFILE` | `personal` | Used when a request sends no `X-Keyring-Profile`. |
| `SPOTIFY_API_KEYRING_TIMEOUT_SECONDS` | `5.0` | Per call to keyring. |
| `SPOTIFY_API_JWKS_CACHE_SECONDS` | `3600` | How long keyring's public keys are trusted. |
| `SPOTIFY_API_JWKS_MIN_REFETCH_SECONDS` | `60` | Floor between refetches an unknown key id may provoke. |
| `SPOTIFY_API_CREDENTIAL_CACHE_SKEW_SECONDS` | `60` | Drop a cached credential this long before it expires. |
| `SPOTIFY_API_CREDENTIAL_CACHE_DEFAULT_TTL_SECONDS` | `300` | Cache lifetime for a credential that reports no expiry. |
| `SPOTIFY_API_SPOTIFY_BASE_URL` | `https://api.spotify.com/v1` | Override for sandboxes. |
| `SPOTIFY_API_REQUEST_TIMEOUT_SECONDS` | `10.0` | Per upstream request. |
| `SPOTIFY_API_MAX_RETRIES` | `3` | Retries for 429/5xx/transport errors. |
| `SPOTIFY_API_RETRY_BACKOFF_BASE_SECONDS` | `0.2` | Exponential, capped at 8s. |
| `SPOTIFY_API_MAX_CONCURRENCY` | `8` | Simultaneous searches per batch. |
| `SPOTIFY_API_MAX_BATCH_SIZE` | `50` | Operational cap on `items`. |
| `SPOTIFY_API_CONFIRM_TIMEOUT_SECONDS` | `15.0` | How long a playback command waits to be confirmed. |
| `SPOTIFY_API_CONFIRM_POLL_INTERVAL_SECONDS` | `0.5` | |
| `SPOTIFY_API_JOB_TTL_SECONDS` | `3600` | How long a finished job stays readable. |
| `SPOTIFY_API_DEFAULT_MARKET` | unset | Applied when a request omits `market`. |
| `SPOTIFY_API_ENVIRONMENT` | `development` | `development`/`test`/`staging`/`production`. |
| `SPOTIFY_API_LOG_LEVEL` | `INFO` | |
| `SPOTIFY_API_LOG_FORMAT` | `json` | `json` or `console`. |

Missing keyring configuration is a **startup failure**, by design — better than
running misconfigured.

## Design notes

- **Identity is verified, not forwarded.** The caller's token is checked locally
  with `keyring-client`, the verifier every service in the family shares, before
  any work starts. The verified `sub` is the only identity this service knows.
- **Every job belongs to one account.** Stored state is never readable without the
  account that created it, and another account's job answers exactly like one that
  never existed.
- **One shared HTTP client.** Created in the app lifespan, closed on shutdown.
  A client per request would discard connection pooling and TLS session reuse,
  which is most of the cost of talking to Spotify.
- **Credential caching with a stampede lock.** A fifty-item batch triggers exactly
  one keyring round trip, not fifty, and a credential is dropped shortly before it
  expires so a refreshed grant is picked up.
- **Field-filtered search.** `track:"Bohemian Rhapsody" artist:"Queen"` rather
  than a bag of words, which also matches tribute covers.
- **Bounded concurrency.** Fifty simultaneous searches would earn an immediate
  429; a semaphore keeps us inside Spotify's tolerance while staying far faster
  than serial resolution.
- **Spotify stays behind a seam.** Routes depend on a `TrackResolver` protocol,
  so nothing outside `spotify/` knows Spotify exists.

## Testing

```bash
make check              # ruff + format + mypy --strict + import-linter + tests at 100% branch coverage
make test-live          # real keyring and Spotify; skips without SPOTIFY_API_KEYRING_* and SPOTIFY_LIVE_USER_TOKEN
```

Coverage is **100%, branch-inclusive, enforced in CI**. Tests are hermetic:
keyring and Spotify are faked at the HTTP transport, and every route test carries
a real signed token, so the suite needs no network and no credentials. See
[docs/testing.md](docs/testing.md).

## Docker

```bash
GITHUB_TOKEN="$(gh auth token)" docker build --secret id=github_token,env=GITHUB_TOKEN -t spotify-api .
docker run --rm -p 8007:8007 --env-file .env spotify-api
```

## Documentation

- [AGENTS.md](AGENTS.md) — operating manual for contributors and agents
- [docs/architecture.md](docs/architecture.md) — layers and request lifecycle
- [docs/api.md](docs/api.md) — full endpoint reference
- [docs/operations.md](docs/operations.md) — environment, readiness, keyring registration
- [docs/testing.md](docs/testing.md) — test taxonomy and fixtures
- [docs/adr/](docs/adr/) — architecture decision records
- [CHANGELOG.md](CHANGELOG.md) — what changed, including breaking configuration renames
- [CONTRIBUTING.md](CONTRIBUTING.md)

## Licence

MIT — see [LICENSE](LICENSE).
