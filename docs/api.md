# HTTP API

Interactive docs at `/docs` when the service is running; `/openapi.json` is the machine-readable
contract. This page covers the parts a schema cannot express.

Base URL `http://localhost:8007`.

## Conventions

- All request and response bodies are JSON. Unknown fields in a request body are **rejected**,
  not ignored.
- Send `X-Request-ID` to supply your own correlation id (≤128 chars); it is echoed on the
  response, stamped on every log line the request produces, and present on every problem
  document. Omit it and a UUID is generated. An over-long id is replaced, not truncated.
- Every non-2xx response is RFC 9457 `application/problem+json`. There is no nested
  `{ "error": { ... } }` envelope.
- Every `/v1` route needs a token. `/healthy` and `/ready` do not.

## Authentication

This service authenticates nobody itself. Callers present a token that **keyring** signed for
this service, this service verifies it locally against keyring's published keys, and the
account comes from that token and from nowhere else.

```
Authorization: Bearer <token from keyring, audience "spotify-api">
```

Mint one with `POST /v1/auth/service-token {"audience": "spotify-api"}` against keyring.
Verification is local — RS256 only, issuer and audience pinned, expiry checked — so there is
no round trip per request. The verified `sub` is the account id; no field, path or query
parameter may name one.

`X-Keyring-User-Token` is still accepted for one release and logged whenever it is used. When
both headers are sent they must carry the same token. `X-Keyring-Profile` chooses which of
*your* keyring profiles to read the Spotify connection from; omit it and
`SPOTIFY_API_KEYRING_DEFAULT_PROFILE` is used.

This service holds **no Spotify credential**. After the token is checked it asks keyring for
the headers to attach for that person, on that profile, and attaches them.

| Response | Means |
| --- | --- |
| `401` | No token, or one this service will not accept. One fixed message whichever rule refused it. Carries `WWW-Authenticate: Bearer`. |
| `403` | Never sent for another account's work. Somebody else's job answers `404`, exactly like one that does not exist. `403` is reserved for Spotify Premium being required. |
| `502` | keyring answered and holds nothing usable for this person on this profile. Reconnect Spotify in keyring. |
| `503` | keyring's keys could not be fetched, or Spotify itself is unusable. The token may be perfectly good; try again shortly. |

## `GET /healthy`

Liveness. Makes no outbound call. Always `200` while the process is serving. Point a
Kubernetes `livenessProbe` at it.

```json
{ "status": "ok", "service": "spotify-api", "version": "0.1.0", "uptime_seconds": 12.34 }
```

## `GET /ready`

Readiness. Fetches keyring's published signing-key document — not keyring's own `/healthy`,
which answers `503` whenever any one person's stored connection has stopped working. The JSON
still reports that check under `dependencies.spotify`: it is the gate on whether this process
can talk to Spotify on anyone's behalf.

**200** — `{ "status": "ready", "dependencies": { "spotify": "ok" } }`

**503** — `{ "status": "not_ready", "dependencies": { "spotify": "unavailable" } }`

Point a `readinessProbe` or load-balancer health check at it.

## `POST /v1/lookup`

Resolve a batch of tracks. Partial success: the response is `200` even when some items fail.
A `4xx`/`5xx` means the *request* failed, not an item.

```bash
curl -sS -X POST localhost:8007/v1/lookup \
  -H "authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"items": [
         {"name": "Bohemian Rhapsody", "artist": "Queen", "album": "A Night at the Opera", "year": 1975},
         {"name": "a track that does not exist"}
       ],
       "market": "GB"}'
```

| Field | Type | Required | Constraints |
| --- | --- | --- | --- |
| `items` | array | yes | 1–50 (see `SPOTIFY_API_MAX_BATCH_SIZE`) |
| `items[].name` | string | yes | 1–200 chars after trimming |
| `items[].artist` | string \| null | no | ≤200 chars; blank → `null` |
| `items[].album` | string \| null | no | ≤200 chars; blank → `null` |
| `items[].year` | integer \| null | no | 1900 – (current year + 1) |
| `market` | string \| null | no | ISO 3166-1 alpha-2; lower-case accepted |

Whitespace is trimmed from all text fields. Explicit `null` behaves like omission. Each
supplied hint becomes a Spotify search filter; `artist` in particular separates an original
from its covers. `market` affects availability and which release is returned.

**200**

```json
{
  "request_id": "0f8c1e2a-...",
  "count": 1,
  "results": [
    {
      "index": 0,
      "query": { "name": "Bohemian Rhapsody", "artist": "Queen", "album": "A Night at the Opera", "year": 1975 },
      "status": "found",
      "track": { "id": "7tFiyTwD0nx5a1eklYtX2J", "name": "Bohemian Rhapsody", "artists": [{"id": "...", "name": "Queen"}], "album": {"id": "...", "name": "...", "release_date": "1975-11-21", "release_year": 1975}, "duration_ms": 354320, "explicit": false, "popularity": 82, "isrc": "GBUM71029604", "preview_url": null, "external_url": "https://open.spotify.com/track/...", "uri": "spotify:track:..." },
      "error": null
    }
  ]
}
```

| Field | Notes |
| --- | --- |
| `request_id` | Matches the `X-Request-ID` response header. |
| `count` | Always equals `results.length` — it is computed, not supplied. |
| `results[].index` | Zero-based position in the submitted array. |
| `results[].query` | The item as received, after validation and trimming. |
| `results[].status` | `found`, `not_found` or `error`. |
| `results[].track` | Populated only when `status` is `found`. |
| `results[].error` | Populated only when `status` is `error`. |

The same Spotify/keyring failures that would be a request-level `502`/`503` appear as per-item
`error` results when they affect only some items. A refused token, a missing Spotify
connection, or keyring being down fail the whole batch — retrying fifty identical errors
would be worse than one honest status.

### Track object

| Field | Type | Notes |
| --- | --- | --- |
| `id` | string | Spotify track ID |
| `name` | string | Title as catalogued by Spotify, which may differ from the query |
| `artists` | array | `{ id, name }`, in Spotify's credit order |
| `album.id` / `album.name` | string | |
| `album.release_date` | string \| null | `YYYY`, `YYYY-MM` or `YYYY-MM-DD` |
| `album.release_year` | integer \| null | Parsed from `release_date`; `null` if unparseable |
| `duration_ms` | integer | |
| `explicit` | boolean | |
| `popularity` | integer \| null | 0–100 |
| `isrc` | string \| null | Absent for some catalogue entries |
| `preview_url` | string \| null | Often `null`; do not depend on it |
| `external_url` | string \| null | `open.spotify.com` page |
| `uri` | string | `spotify:track:...` |

## Player

Reads inspect the caller's Spotify account. Mutations issue a command and then **confirm** it:
Spotify's `204` means the command was accepted, which is not the same as audio playing, so
the response is the player state in which the effect was observed. `POST /v1/player/queue` is
the exception — queuing does not change what is playing, so Spotify's acceptance is the
signal.

| Method | Route | Purpose |
| --- | --- | --- |
| `GET` | `/v1/player` | Current playback state, or `null` when nothing is playing |
| `GET` | `/v1/player/devices` | Devices this account can play on |
| `GET` | `/v1/player/currently-playing` | The currently playing item |
| `GET` | `/v1/player/queue` | What is playing and what is queued behind it |
| `GET` | `/v1/player/recently-played` | Listening history, newest first |
| `POST` | `/v1/player/play` | Start or resume; `uris` or `context_uri` optional |
| `POST` | `/v1/player/pause` | Pause |
| `POST` | `/v1/player/next` | Skip forward (confirmed by the loaded item changing) |
| `POST` | `/v1/player/previous` | Skip backward |
| `POST` | `/v1/player/seek` | Jump to a position in the current item |
| `POST` | `/v1/player/volume` | Set device volume |
| `POST` | `/v1/player/shuffle` | Turn shuffle on or off |
| `POST` | `/v1/player/repeat` | Repeat `off`, `track` or `context` |
| `POST` | `/v1/player/transfer` | Move playback to another device |
| `POST` | `/v1/player/queue` | Queue an item without interrupting playback |

Reads take an optional `market`. Commands take an optional `device_id` (query or body,
depending on the route). No active device is `409`, not `404` — the player exists and is in
the wrong state. Playback control on a free account is `403` `premium_required`. A command
Spotify accepted whose effect never became observable is `504`, with the last seen player
state in `details.observed`.

## Background jobs

Every `/v1` mutation and `POST /v1/lookup` takes `?async=true`. The work is the same coroutine
either way; the flag only chooses whether to wait for it.

- `async=false` (default) — do the work, answer with the result.
- `async=true` — `202` with `job_id`, `poll_url`, `Location: /v1/jobs/{job_id}`. Poll until
  `status` is terminal. For playback, the job is not finished until the effect is confirmed
  on the device.

```json
{ "job_id": "0f8c1e2a-4b5d-4e6f-8a9b-0c1d2e3f4a5b", "status": "pending",
  "operation": "player.play", "poll_url": "/v1/jobs/0f8c1e2a-4b5d-4e6f-8a9b-0c1d2e3f4a5b" }
```

| Method | Route | Purpose |
| --- | --- | --- |
| `GET` | `/v1/jobs` | The caller's recent jobs, newest first. `?status=` and `?limit=` filter. |
| `GET` | `/v1/jobs/{job_id}` | One job and, once it has one, its outcome |
| `DELETE` | `/v1/jobs/{job_id}` | Cancel a running job. A finished job is returned unchanged. |

A job belongs to the account whose verified token started it. Another account's job is `404`,
identical to one that never existed — a job's `result` is the body the synchronous call would
have returned. Jobs live in memory, so they are lost on restart and are not shared between
replicas. They age out after `SPOTIFY_API_JOB_TTL_SECONDS`.

## Errors

Every failure is RFC 9457 `application/problem+json`:

```json
{
  "type": "https://spotify-api.invalid/problems/no-active-device",
  "title": "Conflict",
  "status": 409,
  "detail": "no active Spotify device was found; open Spotify on a device, or pass device_id explicitly",
  "instance": "/v1/player/play",
  "request_id": "0f8c1e2a-..."
}
```

Switch on the last segment of `type`. `request_id` is on every problem, including a 500, and
also in the `X-Request-ID` header. When something fails unexpectedly the detail is
deliberately vague — quote the request id and the logs will have the rest. Validation
failures add `errors`: `{ location, message }` per field, never the offending input.

| Last segment of `type` | Status | Meaning |
| --- | --- | --- |
| `unauthenticated` / `user-token-rejected` | 401 | No token, or one this service will not accept |
| `premium-required` | 403 | Playback control needs Spotify Premium |
| `not-found` / `job-not-found` | 404 | Unknown path, or no such job for this account |
| `method-not-allowed` | 405 | Wrong method; `Allow` says which are accepted |
| `conflict` / `no-active-device` | 409 | Nothing to play on |
| `validation-failed` | 422 | Body violates the schema. `errors` says where |
| `batch-too-large` | 422 | More items than this deployment allows. `details.limit` has the cap |
| `credential-unavailable` / `track-lookup-error` | 502 | No usable Spotify connection, or a lookup that could not be described as an item error |
| `keyring-unavailable` / `spotify-auth-error` / `spotify-rate-limit-error` / `spotify-unavailable-error` | 503 | keyring or Spotify unusable for the whole request |
| `confirmation-timeout` | 504 | Command accepted; effect never observed. `details.observed` is the last player state |
| `internal-server-error` | 500 | Unanticipated. Quote the `request_id` |

A 401 always carries `WWW-Authenticate: Bearer`.
