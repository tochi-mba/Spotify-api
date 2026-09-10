# AGENTS.md

Operating manual for anyone — human or agent — changing this repository.
Read this before your first commit here. It is normative: where it says
**must**, CI enforces it.

---

## 1. What this service is

An HTTP API that resolves batches of loosely-specified tracks against the
Spotify Web API. A caller submits an array of `{ name, artist?, album?, year? }`
and receives one result per item, in order, each with its own status.

It is intentionally small and does one thing. Resist the urge to grow it into a
general Spotify proxy.

## 2. The three invariants

Break any of these and the change is wrong, however well it is written.

**2.1 — Partial success.** One item failing must never fail the batch. Every
per-item failure is caught in `SpotifyTrackResolver._resolve_item` and rendered
as a result with `status="error"`. A new failure mode means a new branch there,
never an exception escaping to the route.

**2.2 — Spotify stays behind the seam.** Only `src/spotify_api/spotify/` may
import `httpx`, know a Spotify URL, or understand Spotify's JSON. Routes depend
on the `TrackResolver` protocol in `spotify/protocols.py`. If you find yourself
importing `SpotifyClient` into a route, stop and reconsider.

**2.3 — The contract lives in the models.** Every rule a caller must satisfy
belongs in `models/requests.py`, not in a handler. That way it is enforced once
and published automatically in the OpenAPI schema. The single exception is the
*operational* batch cap, which is per-deployment configuration and so is checked
in the handler.

## 3. Test-driven development is mandatory

Every change starts with a failing test. Not "write the code then add a test" —
the test comes first, you watch it fail, and you make it pass.

```
1. Write the test. Run it. Confirm it fails, and that it fails for the reason
   you expect. A test that passes before the code exists is testing nothing.
2. Write the smallest code that makes it pass.
3. Refactor with the test green.
4. Run the full gate (§4) before committing.
```

This is not ceremony. Two real bugs in this codebase's short history were found
by tests written before the fix, and would have shipped otherwise:

- `JsonFormatter` resolved the request id at *format* time, so any deferred
  formatting lost it. The id is now stamped onto the `LogRecord` at creation.
- A `401` with `max_retries=0` fell out of the retry loop and surfaced as
  `SpotifyRateLimitError`. Re-authentication is now separate from the budget.

Neither was visible by reading the code. Both were obvious the moment a test
asked the awkward question.

## 4. The gate

All four must pass. CI runs exactly this.

```bash
make check          # or, individually:
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

Beware `cmd | tail` in a shell chain — `tail`'s exit code masks the failure.
Use `make check`, which does not.

## 5. The coverage rule

**Coverage is 100%, branch-inclusive, enforced by `--cov-fail-under=100`.**

- Never lower `fail_under`. Not temporarily, not "just to unblock".
- Never add `# pragma: no cover` to dodge the gate. The sanctioned exclusions
  are declared once in `pyproject.toml` (`TYPE_CHECKING` blocks, protocol
  bodies, `NotImplementedError`, the `__main__` guard) and that list does not
  grow without a very good reason stated in the commit message.
- An uncovered line is the suite telling you about a case you have not thought
  about. Every gap found while building this was a real one worth a test —
  explicit JSON nulls, a JSON body that is not an object, a token refresh with
  no retry budget. Treat the next gap the same way.

If a line is genuinely unreachable, delete it.

## 6. Layout

```
src/spotify_api/
├── app.py            create_app() factory; the lifespan owns ONE httpx client
├── config.py         pydantic-settings; credentials are SecretStr, always
├── logging.py        JSON logs; request id stamped at record creation
├── middleware.py     request id in/out, one access log line per request
├── errors.py         exception hierarchy + the handlers that render it
├── api/
│   ├── dependencies.py   DI providers; tests override these
│   ├── router.py         health at root, everything else under /v1
│   └── routes/           one module per resource
├── models/           the public contract, request and response
└── spotify/          the adapter. Nothing outside here knows Spotify exists.
    ├── protocols.py      the seam routes depend on
    ├── auth.py           token cache + stampede lock
    ├── client.py         the retry ladder
    ├── query.py          Spotify field filters
    ├── mappers.py        raw JSON -> models, defensively
    └── resolver.py       bounded-concurrency batch resolution

tests/
├── unit/         one module per source module, no I/O
├── integration/  routes via ASGI transport, fake resolver injected
├── contract/     assumptions about Spotify's response shape
├── live/         real API. Deselected by default, skipped without credentials.
├── fixtures/     recorded Spotify payloads
└── factories.py  make_settings() — always build settings through this
```

## 7. Recipes

### Add a field to the request

1. Test in `tests/unit/test_models_requests.py` for the valid case, the invalid
   case, blank, and explicit `null`. Watch them fail.
2. Add the field to `LookupItem` with a `description` — it becomes OpenAPI docs.
3. If it should narrow the search, add a filter in `spotify/query.py` **and** a
   test in `tests/unit/test_query.py` proving it is omitted when absent.

### Add a route

1. Test in `tests/integration/`, using the `client` and `resolver` fixtures.
2. Add the handler under `api/routes/`, with `summary`, `description` and a
   `responses` map for every non-200 it can produce.
3. Include it in `api/router.py` — under `api_router` unless it is a probe.
4. Depend on `ResolverDep`/`SettingsDep`, never on a concrete class.

### Add a Spotify endpoint

1. Extend `SpotifyClient` with a method that goes through
   `_get_with_retries`. Do not hand-roll a second retry ladder.
2. Add a mapper, and a recorded fixture in `tests/fixtures/`.
3. Add contract tests pinning the shape you now depend on.
4. If routes need it, extend `TrackResolver` — or add a sibling protocol.

### Re-record a fixture

Capture the real response, redact nothing (these are public catalogue data),
save it under `tests/fixtures/`, then run the contract suite. If it fails, the
mapper and the fixture have drifted and you must decide which is wrong.

## 8. Standards

- **Type hints everywhere.** `mypy --strict` over `src` *and* `tests`.
- **Docstrings** on every public module, class and function. Google style.
  Say *why*, not *what* — the code says what.
- **`from __future__ import annotations`** at the top of every module.
- **No relative imports.** Enforced by ruff.
- **Comments earn their place.** Explain a decision or a trap. A comment
  restating the next line is noise; a comment saying why `2.0**attempt` rather
  than `2**attempt` saves the next person twenty minutes.
- **Never log a secret.** Credentials are `SecretStr`; keep them that way. A
  test asserts they survive neither `repr()` nor `model_dump_json()`.
- **Never leak internals to clients.** Unexpected failures return an opaque
  message; the detail goes to the logs.
- **Inject time and sleep.** `clock` and `sleeper` parameters exist so expiry
  and backoff are asserted, not waited for. Do not call `time.time()` or
  `asyncio.sleep()` directly in new adapter code.

## 9. Configuration

All settings are environment variables, parsed once in `config.py`. Adding one
means: a field with a `description`, a range constraint if it is numeric, a test
for its default and its bounds, a row in the README table, and a line in
`.env.example`.

`SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET` are required and have no
defaults — the service must fail to start rather than run misconfigured.

## 10. Commits

- Conventional prefixes: `feat:`, `fix:`, `test:`, `docs:`, `chore:`, `refactor:`.
- The subject says what changed; the body says **why**, and calls out anything
  surprising — a bug found, a trade-off taken, a thing deliberately not done.
- One logical change per commit. The TDD history should be readable.

## 11. Definition of done

- [ ] A failing test was written first.
- [ ] `make check` passes: ruff, format, mypy `--strict`, pytest at 100%.
- [ ] `fail_under` untouched; no new `# pragma: no cover`.
- [ ] New config documented in README and `.env.example`.
- [ ] New endpoints carry OpenAPI `summary`, `description` and `responses`.
- [ ] No secret can reach a log or a response body.
- [ ] The commit message explains why.
