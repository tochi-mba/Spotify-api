# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
