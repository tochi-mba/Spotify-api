# Operations

## Running it

```bash
make run                                   # dev, :8007, reload
uv run spotify-api                         # production entry point
docker build -t spotify-api:local . && docker run --rm -p 8007:8007 --env-file .env spotify-api:local
```

The process binds `SPOTIFY_API_HOST:SPOTIFY_API_PORT`, `127.0.0.1:8007` by default; the image sets the host to `0.0.0.0`. Change the bind in a
reverse proxy, not here.

`GET /healthy` returns `200` from process state alone. Point a liveness probe at it. It is one
of two routes that do not need a token; the other is `/ready`.

Nothing starts without keyring. Set `SPOTIFY_API_KEYRING_BASE_URL` and
`SPOTIFY_API_KEYRING_SERVICE_TOKEN`. Missing either is a startup failure, by design.

## Registering with keyring

This service's name in keyring is `spotify-api`. Both sides must agree or every token is
refused.

1. Add an entry to keyring's `KEYRING_SERVICE_TOKENS` JSON map:

   ```json
   { "spotify-api": "<at least 32 random characters>" }
   ```

   Generate one with `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
2. Put the **same** value in `SPOTIFY_API_KEYRING_SERVICE_TOKEN`.
3. Keep `SPOTIFY_API_KEYRING_AUDIENCE` at `spotify-api` (the default) unless you also rename
   the keyring map entry. Keyring's internal endpoints refuse a user token minted for any
   other audience.
4. Keep `SPOTIFY_API_KEYRING_ISSUER` equal to keyring's own `KEYRING_ISSUER`.
5. Each person connects Spotify on a keyring profile
   (`POST /v1/profiles/{profile}/connections/spotify/authorize`) and mints a short-lived
   token with `POST /v1/auth/service-token {"audience": "spotify-api"}`.

A mismatch of issuer or audience refuses every token with the same message every other bad
token gets, so check these first when a working deployment suddenly accepts nobody.

## Configuration

Every setting is an environment variable prefixed `SPOTIFY_API_`. **Unknown variables under
the prefix are rejected at startup**, so a typo fails loudly instead of leaving a default
silently in place. Distinctive names this service read before it had a prefix (for example
`KEYRING_BASE_URL`, `SPOTIFY_API_BASE_URL`, `DEFAULT_MARKET`) are also a startup error; the
message names the replacement.

### Identity

| Variable | Default | Notes |
| --- | --- | --- |
| `SPOTIFY_API_KEYRING_BASE_URL` | — | **Required.** Where keyring is. Trailing slashes are stripped. |
| `SPOTIFY_API_KEYRING_SERVICE_TOKEN` | — | **Required.** This service's entry, under `spotify-api`, in keyring's `KEYRING_SERVICE_TOKENS` JSON map. At least 32 characters. Held as a `SecretStr`. |
| `SPOTIFY_API_KEYRING_ISSUER` | `http://127.0.0.1:8001` | Must equal keyring's own `KEYRING_ISSUER`, or every token is refused. |
| `SPOTIFY_API_KEYRING_AUDIENCE` | `spotify-api` | Must equal the audience callers mint tokens for, and this service's name in `KEYRING_SERVICE_TOKENS`. |
| `SPOTIFY_API_KEYRING_DEFAULT_PROFILE` | `personal` | Credential profile used when a request sends no `X-Keyring-Profile`. |
| `SPOTIFY_API_KEYRING_TIMEOUT_SECONDS` | `5.0` | Per call to keyring. `>0`, `≤60`. |
| `SPOTIFY_API_JWKS_CACHE_SECONDS` | `3600` | How long public keys are held before re-reading. `>0`, `≤86400`. |
| `SPOTIFY_API_JWKS_MIN_REFETCH_SECONDS` | `60` | Floor between key refetches an unknown key id may provoke. Not a tuning knob. `>0`, `≤3600`. |
| `SPOTIFY_API_CREDENTIAL_CACHE_SKEW_SECONDS` | `60` | Drop a cached Spotify credential this many seconds before it expires. `0–600`. |
| `SPOTIFY_API_CREDENTIAL_CACHE_DEFAULT_TTL_SECONDS` | `300` | Cache lifetime when keyring reports no expiry. `0–3600`. |

Two settings must agree with keyring or nothing works, and both fail closed: `KEYRING_ISSUER`
and `KEYRING_AUDIENCE`.

### Upstream and resilience

| Variable | Default | Notes |
| --- | --- | --- |
| `SPOTIFY_API_SPOTIFY_BASE_URL` | `https://api.spotify.com/v1` | Override for sandboxes and tests. Trailing slashes are stripped. |
| `SPOTIFY_API_REQUEST_TIMEOUT_SECONDS` | `10.0` | Per upstream request. `>0`, `≤120`. |
| `SPOTIFY_API_MAX_RETRIES` | `3` | Retries for 429 / 5xx / transport errors. `0–10`. |
| `SPOTIFY_API_RETRY_BACKOFF_BASE_SECONDS` | `0.2` | Exponential, capped at 8s. `0–10`. |
| `SPOTIFY_API_MAX_CONCURRENCY` | `8` | Simultaneous searches per batch. `1–64`. |
| `SPOTIFY_API_MAX_BATCH_SIZE` | `50` | Operational cap on `items`. `1–200`. The schema's absolute ceiling is separate. |

### Work

| Variable | Default | Notes |
| --- | --- | --- |
| `SPOTIFY_API_CONFIRM_TIMEOUT_SECONDS` | `15.0` | How long a playback command waits to be confirmed on the device. `>0`, `≤300`. |
| `SPOTIFY_API_CONFIRM_POLL_INTERVAL_SECONDS` | `0.5` | Pause between confirmation polls. `>0`, `≤30`. |
| `SPOTIFY_API_JOB_TTL_SECONDS` | `3600` | How long a finished job stays readable. Jobs are in-memory. `>0`, `≤86400`. |
| `SPOTIFY_API_DEFAULT_MARKET` | unset | ISO 3166-1 alpha-2 applied when a request omits `market`. |

### Observability

| Variable | Default | Notes |
| --- | --- | --- |
| `SPOTIFY_API_ENVIRONMENT` | `development` | `development` / `test` / `staging` / `production`. |
| `SPOTIFY_API_LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL`. |
| `SPOTIFY_API_LOG_FORMAT` | `json` | `json` in deployment, `console` for human-readable local output. |

## Readiness

`GET /ready` fetches keyring's published signing-key document. It returns `200` when that
document is reachable and `503` when it is not. It does **not** obtain a Spotify access
token, and it does **not** call keyring's `/healthy`.

The JSON still names the check `dependencies.spotify`. Treat `unavailable` as "this replica
cannot currently serve anyone", not as "Spotify the product is down".

The Docker `HEALTHCHECK` probes `/healthy`, not `/ready`, so a keyring blip does not restart
the container.

## Exposing it

This service is built to be reachable by a handful of people over the internet. It is not
built to be reachable by everyone. Work down this list before it is:

1. **Terminate TLS at a reverse proxy** (Caddy, nginx, Traefik) and let it hold the
   certificate. This service speaks plain HTTP and should bind behind the proxy. A bearer
   token over plain HTTP is a bearer token in somebody's transit logs.
2. **Check `/ready` is `200` against a real keyring**, not against a stub.
3. **Set both keyring settings from a secret store, not from a shell.**
   `SPOTIFY_API_KEYRING_SERVICE_TOKEN` is a shared secret; it is held as a `SecretStr` so it
   cannot be printed with the rest of the settings, and it should not reach a shell history
   either.
4. **Consider blocking `/docs`, `/redoc` and `/openapi.json` at the proxy.** They are open so
   the interactive docs work in a browser. They describe the service rather than anybody using
   it, but there is no reason to publish even that to the whole internet.

What the service does for itself:

- **Every `/v1` route needs a token**, checked before the handler runs.
- **One account cannot see another's jobs.** Cross-account access answers `404`, never `403`.
- **Credentials are resolved per request and stored nowhere** — not on a job, not in a log
  record, not in any response. Cached headers are keyed by a hash of the user token, not the
  token itself.
- Unexpected error text is never returned to callers; it can carry URLs or credentials.
  Callers get a request id that ties the response to the full log record.

What remains your problem:

- Jobs are in-memory. A restart forgets them, and two replicas do not share them. Front a
  single replica or do not use `?async=true` if that matters.
- Spotify rate limits are per user connection, not per this process. `MAX_CONCURRENCY` is a
  courtesy, not a quota you control.

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| Startup fails with "unrecognised configuration" | A typo under `SPOTIFY_API_`, or a distinctive old name (`KEYRING_BASE_URL`, `SPOTIFY_API_BASE_URL`, …). The message names the replacement. |
| Startup fails with a short service token | `SPOTIFY_API_KEYRING_SERVICE_TOKEN` is under 32 characters, the rule keyring itself enforces. |
| Every request answers `401` on a deployment that worked | `KEYRING_ISSUER` or `KEYRING_AUDIENCE` no longer matches keyring. Both fail closed and say nothing more. Also: callers still sending only `X-Keyring-User-Token` after that header is removed. |
| `/ready` is `503` | keyring's JWKS document cannot be fetched. Check the base URL and that keyring is up. Cached keys survive an outage; a cold start does not. |
| Lookups answer `502` `credential-unavailable` | That account has not connected Spotify on that profile, or the grant was revoked. Reconnect it in keyring. |
| Playback answers `409` | Nothing is awake. Open Spotify on a device, or pass `device_id`. |
| Playback answers `403` `premium-required` | The Spotify account is free. Playback control needs Premium. |
| Playback answers `504` | Spotify accepted the command; the device never showed the effect. `details.observed` is the last player state. |
| Jobs vanish after a restart | Expected — the job store is in-memory. |
| Logs are not JSON despite `LOG_FORMAT=json` | The process was started before the setting was applied; logging is configured in `create_app`. Restart. |
