# API reference

Base URL `http://localhost:8000`. Interactive docs at `/docs`, schema at
`/openapi.json`.

## Conventions

- All request and response bodies are JSON.
- Send `X-Request-ID` to supply your own correlation id (≤128 chars); it is
  echoed on the response and stamped on every log line the request produces.
  Omit it and a UUID is generated.
- Unknown fields in a request body are **rejected**, not ignored.
- Every non-2xx response uses the error envelope below.

---

## `GET /healthy`

Liveness. Makes no outbound call.

**200**

```json
{ "status": "ok", "service": "spotify-api", "version": "0.1.0", "uptime_seconds": 12.34 }
```

Never returns anything else while the process is serving. Suitable for a
Kubernetes `livenessProbe`.

---

## `GET /ready`

Readiness. Verifies a Spotify access token can be obtained.

**200** — `{ "status": "ready", "dependencies": { "spotify": "ok" } }`

**503** — `{ "status": "not_ready", "dependencies": { "spotify": "unavailable" } }`

Suitable for a `readinessProbe` or a load balancer health check.

---

## `POST /v1/lookup`

Resolve a batch of tracks.

### Request

```json
{
  "items": [
    { "name": "Bohemian Rhapsody", "artist": "Queen", "album": "A Night at the Opera", "year": 1975 }
  ],
  "market": "GB"
}
```

| Field | Type | Required | Constraints |
|---|---|---|---|
| `items` | array | yes | 1–50 (see `MAX_BATCH_SIZE`) |
| `items[].name` | string | yes | 1–200 chars after trimming |
| `items[].artist` | string \| null | no | ≤200 chars; blank → `null` |
| `items[].album` | string \| null | no | ≤200 chars; blank → `null` |
| `items[].year` | integer \| null | no | 1900 – (current year + 1) |
| `market` | string \| null | no | ISO 3166-1 alpha-2; lower-case accepted |

Notes:

- Whitespace is trimmed from all text fields.
- Explicit `null` behaves exactly like omission.
- Each supplied hint becomes a Spotify search filter, which materially improves
  accuracy: `artist` in particular separates an original from its covers.
- `market` affects availability and which release is returned.

### Response `200`

```json
{
  "request_id": "0f8c1e2a-...",
  "count": 1,
  "results": [
    {
      "index": 0,
      "query": { "name": "Bohemian Rhapsody", "artist": "Queen", "album": "A Night at the Opera", "year": 1975 },
      "status": "found",
      "track": { "...": "see below" },
      "error": null
    }
  ]
}
```

| Field | Notes |
|---|---|
| `request_id` | Matches the `X-Request-ID` response header. |
| `count` | Always equals `results.length` — it is computed, not supplied. |
| `results[].index` | Zero-based position in the submitted array. |
| `results[].query` | The item as received, after validation and trimming. |
| `results[].status` | `found`, `not_found` or `error`. |
| `results[].track` | Populated only when `status` is `found`. |
| `results[].error` | Populated only when `status` is `error`. |

**Partial success:** the response is `200` even when some items fail. Check
`status` per item. A `4xx`/`5xx` means the *request* failed, not an item.

### Track object

| Field | Type | Notes |
|---|---|---|
| `id` | string | Spotify track ID |
| `name` | string | Title as catalogued by Spotify, which may differ from your query |
| `artists` | array | `{ id, name }`, in Spotify's credit order |
| `album.id` | string | |
| `album.name` | string | |
| `album.release_date` | string \| null | `YYYY`, `YYYY-MM` or `YYYY-MM-DD` |
| `album.release_year` | integer \| null | Parsed from `release_date`; `null` if unparseable |
| `duration_ms` | integer | |
| `explicit` | boolean | |
| `popularity` | integer \| null | 0–100 |
| `isrc` | string \| null | Absent for some catalogue entries |
| `preview_url` | string \| null | Often `null`; do not depend on it |
| `external_url` | string \| null | `open.spotify.com` page |
| `uri` | string | `spotify:track:...` |

### Errors

```json
{
  "error": {
    "type": "validation_error",
    "message": "the request body is invalid",
    "details": { "errors": [{ "type": "string_too_short", "loc": ["body", "items", 0, "name"] }] },
    "request_id": "0f8c1e2a-..."
  }
}
```

| `type` | Status | Cause | What to do |
|---|---|---|---|
| `validation_error` | 422 | Body violates the schema | Fix the body; `details.errors` says where |
| `batch_too_large` | 422 | More items than configured | Split the batch; `details.limit` has the cap |
| `spotify_auth_error` | 503 | Spotify rejected our credentials | Operator issue — check the deployment's credentials |
| `spotify_rate_limit_error` | 503 | Rate limited past the retry budget | Back off and retry |
| `spotify_unavailable_error` | 503 | Spotify unreachable or failing | Retry later |
| `internal_error` | 500 | Unanticipated | Report it with the `request_id` |

The same `spotify_*` conditions appear as per-item `error` results when they
affect only some items, and as a request-level failure only when the batch could
not be attempted at all.

### Status codes

| Code | Meaning |
|---|---|
| 200 | Batch processed. Inspect per-item `status`. |
| 405 | Wrong method — this endpoint is POST only. |
| 422 | Body invalid, or batch over the operational cap. |
| 500 | Unanticipated failure. |
| 503 | Spotify unusable for the whole request. |
