# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **settings-client 0.4.1.** A single-flight lock is dropped by the last caller out. Older
  clients kept the lock of every resolve that failed (an outage, a refused grant) for good,
  one per token, and keyring tokens rotate every few minutes.
- **A person's settings are read for the profile the request runs as.** Every `spotify`
  setting is profile-scoped, and settings-api returns a profile's values only to a request
  that names the profile. This service named none, so every choice a person made arrived
  as the catalogue default: market, batch size, confirm timeout, shuffle, repeat, and
  `allow_explicit`, so explicit tracks were played to someone who had turned them off. The
  request's `X-Keyring-Profile` is now named, or with none the person's
  `common.default_profile`, read first. settings-client moves to 0.4.0, whose test fake
  keeps profiles apart; the old one ignored them, which is why no test caught this.
- Apply `spotify.allow_explicit` to track lookup, play and queue. Clean lookup candidates
  are preferred when disabled; explicit named tracks are refused before player commands.
  Requests requiring an unavailable explicit-content preference fail rather than guess.

### Security

- Every request's keyring user token is now verified locally against keyring's published
  keys before any work starts: RS256 only, issuer and audience (`spotify-api`) pinned, every
  required claim present, expiry checked. Previously the token was forwarded to keyring
  unverified, so this service never knew which account a request belonged to.
- Background jobs belong to the account that started them. `GET /v1/jobs`,
  `GET /v1/jobs/{job_id}` and `DELETE /v1/jobs/{job_id}` now require a verified user token
  and see only the caller's own jobs; another account's job answers `404`, exactly like an
  unknown one. Previously any caller could list, read and cancel every account's jobs,
  results included.

### Changed

- **Breaking:** credentials come from keyring. This service holds no Spotify credential
  and no longer uses Client Credentials: `SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET`
  are replaced by keyring's base URL and this service's token there (now
  `SPOTIFY_API_KEYRING_BASE_URL` and `SPOTIFY_API_KEYRING_SERVICE_TOKEN`), and every route
  but the health probes needs the caller's keyring user token. The Spotify credential
  keyring hands back is cached per profile under a hash of the user token, so a batch
  costs one keyring call, and a `401` from Spotify drops it so keyring resolves again.
  Keyring refusing or failing answers `credential_unavailable` or `keyring_unavailable`
  for the whole request rather than an error per item.
- **Breaking:** the floor is now **Python 3.12** (CI runs 3.12 and 3.13).
  `.python-version`, `requires-python`, ruff's `target-version`, mypy's `python_version`,
  the Docker base image and the pre-commit interpreter all moved together, and `uv.lock`
  was regenerated. The family-wide reason is in the meta-repo's
  [ADR-0008](https://github.com/tochi-mba/LUCY-assistant/blob/main/docs/adr/0008-python-3-12-floor.md):
  `weftai`, which the assistant hub depends on, requires 3.12 and uses PEP 695 type
  parameters that do not parse on 3.11. Generics here moved to PEP 695 syntax with it.
- CI gets a short-lived token from the family token broker over OIDC (`id-token: write`)
  rather than inheriting a shared credential; image builds accept a BuildKit
  `github_token` secret so tagged client packages can be fetched from private family
  repositories.
  `make docker` uses the signed-in GitHub account without saving its token in an image.
- **Breaking:** every environment variable is now prefixed `SPOTIFY_API_`, and
  `SPOTIFY_API_BASE_URL` is now `SPOTIFY_API_SPOTIFY_BASE_URL`. The bare `KEYRING_*` names
  sat inside keyring's own prefix, which keyring refuses, so a host configuring both services
  could not start keyring. The distinctive old names are refused at startup with their
  replacements; the generic ones (`ENVIRONMENT`, `LOG_LEVEL`, `LOG_FORMAT`, `MAX_RETRIES`,
  `MAX_CONCURRENCY`, `REQUEST_TIMEOUT_SECONDS`, `RETRY_BACKOFF_BASE_SECONDS`) are simply no
  longer read, because other software sets them.
- **Breaking:** `SPOTIFY_API_KEYRING_SERVICE_TOKEN` must be at least 32 characters, the rule
  keyring itself enforces. Unknown `SPOTIFY_API_*` variables are a startup error.
- **Breaking:** every non-2xx response is RFC 9457 `application/problem+json` rather than the
  nested `{ "error": { "type", "message", "details", "request_id" } }` envelope. Switch on the
  last segment of `type`. A 401 carries `WWW-Authenticate: Bearer`.
- The canonical way to send the keyring user token is `Authorization: Bearer <token>`.
  `X-Keyring-User-Token` is still accepted for one release and logged whenever it is used;
  when both headers are sent they must carry the same token.
- Token verification comes from `keyring-client`, the library every service in the family
  shares, rather than code of this service's own.
- Logging is structlog. Every record is redacted for secret-looking fields before it is
  rendered; an exception is logged as its stack and type name, never its message.
- Makefile targets match the family: `fmt`, `lint`, `type`, `imports`, `test`, `test-live`
  (was `live`), `cov`, `check`, `run`, `docker`, `clean`. `make check` is now
  `lint type imports test`.

### Added

- **A new play takes the person's shuffle and repeat.** `spotify.shuffle_on_play` and
  `spotify.repeat_mode` could be set and read back, and changed nothing. A play that names
  what to play now sets them once playback is confirmed, and confirms them too. `POST
  /v1/player/play` takes `shuffle` and `repeat` for a request that wants to say, and a
  request that says wins. The settings only ever turn a mode on: left off they send Spotify
  nothing, so a shuffle chosen in the app is not undone, and a resume never applies them.
- A GitHub Pages site at <https://tochi-mba.github.io/Spotify-api/>, in the REX ink/signal style: what Spotify-api is,
  its API, how to run it and what it will not do. `site/` is plain static HTML;
  `.github/workflows/pages.yml` publishes it after `scripts/check_site.py` has checked every
  page for a broken anchor, a missing asset, an image without alt text or draft text.
- The repository is attributed to REX Technologies: the LICENSE copyright holder, the package
  author and the README.
- The Player API. Reads: `GET /v1/player` (playback state), `/v1/player/devices`,
  `/v1/player/currently-playing`, `/v1/player/queue` and `/v1/player/recently-played`.
  Commands: `POST /v1/player/play`, `pause`, `next`, `previous`, `seek`, `volume`,
  `shuffle`, `repeat`, `transfer` and `queue`. Spotify answers a command `204`, which
  means accepted, not done, so each command polls the player until its effect is seen
  (for `play` with `uris`, that exact track playing) and answers with that state; after
  `SPOTIFY_API_CONFIRM_TIMEOUT_SECONDS` (15) it fails `504 confirmation_timeout` with the
  last state seen. Queueing is the exception: it leaves playback alone, so Spotify's
  acceptance is all there is. A free account answers `403 premium_required`, and no
  device to play on `409 no_active_device`.
- Background jobs. `?async=true` on `POST /v1/lookup` and every player command answers
  `202` with a job id and a `Location` header, and runs the same work in the background.
  `GET /v1/jobs`, `GET /v1/jobs/{job_id}` and `DELETE /v1/jobs/{job_id}` list, poll and
  cancel them. Jobs live in memory, are lost on restart, and are dropped
  `SPOTIFY_API_JOB_TTL_SECONDS` (an hour) after they were created.
- `GET /v1/jobs/{job_id}?wait_seconds=` (0-60) holds the request open until the job
  finishes, so a caller need not poll in a loop.
- `docs/mcp.md`: what a model may call through this service and how results are framed.
- `SPOTIFY_API_KEYRING_ISSUER`, `SPOTIFY_API_KEYRING_AUDIENCE`, `SPOTIFY_API_JWKS_CACHE_SECONDS`
  and `SPOTIFY_API_JWKS_MIN_REFETCH_SECONDS`.
- import-linter contracts: models and jobs must not import the API layer; `keyring_client`
  is imported only from config validation, the app factory, the auth dependency and the
  credentials package; `httpx` stays out of the API and models layers.
- `docs/operations.md`. `.python-version`, `CLAUDE.md`, `.pre-commit-config.yaml`,
  `.editorconfig`.

### Fixed

- `/ready` probed a keyring path that does not exist (`/healthz`), so it never reported ready
  against a real keyring. It now probes keyring's published key document -- not keyring's own
  `/healthy`, which answers `503` whenever any one person's stored connection has stopped
  working.

### Removed

- Dead code found by the family sweep, none of it reachable from a request:
  `Settings.is_production`, `JobRunner.in_flight` and `SpotifyResponse.is_empty` were each
  referenced only by the unit test that covered them, and the `SettingsDep` alias was
  exported but no route depended on it, which left `get_settings_dependency` used only by its
  own test and an override in the integration suite that nothing consulted; both went with
  it. `Settings.environment` is still read and logged at startup.

## [0.1.0] — 2026-09-10

Initial release.

### Added

- `POST /v1/lookup` — resolve a batch of `{ name, artist?, album?, year? }`
  items against the Spotify Web API, answering with partial success: one result
  per submitted item, in order, each with its own `found` / `not_found` /
  `error` status.
- `GET /healthy` — liveness, making no outbound call.
- `GET /ready` — readiness, verifying a Spotify access token can be obtained.
- Spotify adapter: Client Credentials auth with a stampede-safe token cache,
  field-filtered search, a retry ladder covering 401/403/429/5xx/timeouts, and
  bounded-concurrency batch resolution.
- Structured JSON logging with request-id correlation, accepted and echoed via
  `X-Request-ID`.
- One error envelope for every non-2xx response.
- OpenAPI schema with examples, served at `/docs`.
- 100% branch coverage enforced in CI across Python 3.11, 3.12 and 3.13.
- Multi-stage Dockerfile running as a non-root user, with a liveness healthcheck.

[Unreleased]: https://github.com/tochi-mba/Spotify-api/compare/de182f1bb1f7388cb7f6ff0e87991797834ceadc...HEAD
[0.1.0]: https://github.com/tochi-mba/Spotify-api/tree/de182f1bb1f7388cb7f6ff0e87991797834ceadc
