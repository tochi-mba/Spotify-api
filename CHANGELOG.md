# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

- **Breaking:** the floor is now **Python 3.12** (CI runs 3.12 and 3.13).
  `.python-version`, `requires-python`, ruff's `target-version`, mypy's `python_version`,
  the Docker base image and the pre-commit interpreter all moved together, and `uv.lock`
  was regenerated. The family-wide reason is in the meta-repo's
  [ADR-0008](https://github.com/tochi-mba/LUCY-assistant/blob/main/docs/adr/0008-python-3-12-floor.md):
  `weftai`, which the assistant hub depends on, requires 3.12 and uses PEP 695 type
  parameters that do not parse on 3.11. Generics here moved to PEP 695 syntax with it.
- CI inherits `FAMILY_GITHUB_TOKEN`; image builds accept a BuildKit `github_token`
  secret so tagged client packages can be fetched from private family repositories.
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

[Unreleased]: https://github.com/tochi-mba/Spotify-api/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/tochi-mba/Spotify-api/releases/tag/v0.1.0
