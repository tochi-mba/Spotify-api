# Testing

## The gate

```bash
make check   # ruff, format, mypy --strict, pytest at 100% branch coverage
```

Coverage is 100%, branch-inclusive, enforced by `--cov-fail-under=100` in
`pyproject.toml`. See §5 of [AGENTS.md](../AGENTS.md) for why it is not
negotiable.

## Taxonomy

| Suite | Scope | Fakes | Speed |
|---|---|---|---|
| `tests/unit/` | one module each, in isolation | collaborators | instant |
| `tests/integration/` | routes through the ASGI transport | the resolver | fast |
| `tests/integration/test_end_to_end.py` | the whole real graph | only the network | fast |
| `tests/contract/` | assumptions about Spotify's response shape | recorded fixtures | instant |
| `tests/live/` | the real Spotify API | nothing | slow, opt-in |

The end-to-end module is the interesting one: it builds the *production* object
graph — route, resolver, client, token provider, mappers — and fakes only the
network, via `create_app(transport=httpx.MockTransport(...))`. Everything else
injects a fake resolver, so it proves the HTTP contract without proving the
wiring; this proves the wiring.

## Running

```bash
uv run pytest                          # everything except live
uv run pytest tests/unit -q            # one suite
uv run pytest -k stampede              # one behaviour
uv run pytest -m live                  # the real API (needs credentials)
make cov && open htmlcov/index.html    # where the gaps are
```

Live tests are deselected by default and skip outright without
`SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET`, so the full suite runs on a
machine that has never seen a Spotify credential.

## Conventions

- **Test names are sentences.** `test_concurrent_callers_trigger_exactly_one_token_fetch`
  tells you what broke without opening the file.
- **Build settings with `make_settings()`** from `tests/factories.py`. It supplies
  dummy credentials and disables `.env` loading, so tests never depend on the
  machine they run on.
- **Fake at the transport, not the library.** `httpx.MockTransport` exercises the
  real request-building and response-parsing code. Patching `SpotifyClient.search`
  would test the mock instead.
- **Inject time and sleep.** `FakeClock` and `RecordingSleeper` mean expiry and
  backoff are asserted rather than waited for. The suite runs in seconds despite
  covering a full retry ladder.
- **Assert on behaviour, not implementation** — except where the implementation
  *is* the behaviour, such as "exactly one token fetch" or "peak concurrency ≤ 3".

## Fixtures

Recorded Spotify payloads live in `tests/fixtures/` and are loaded by
`tests/conftest.py`, so unit, integration and contract tests all assert against
the same bytes.

To refresh one:

```bash
TOKEN=$(curl -s -X POST https://accounts.spotify.com/api/token \
  -H "Authorization: Basic $(printf '%s:%s' "$SPOTIFY_CLIENT_ID" "$SPOTIFY_CLIENT_SECRET" | base64 -w0)" \
  -d grant_type=client_credentials | jq -r .access_token)

curl -s -G https://api.spotify.com/v1/search \
  -H "Authorization: Bearer $TOKEN" \
  --data-urlencode 'q=track:"Bohemian Rhapsody" artist:"Queen"' \
  -d type=track -d limit=1 | jq > tests/fixtures/search_track_found.json
```

Then run `uv run pytest tests/contract`. If it fails, the fixture and the
mappers have drifted apart — decide which is wrong before changing either.

These are public catalogue data, so there is nothing to redact. Never commit a
fixture containing a token: the token endpoint's responses are constructed
inline in tests, never recorded.

## Writing a good failing test

Watch it fail, and check *why*. A test that passes before the code exists is
testing nothing, and a test that fails with `ImportError` has not yet told you
anything about behaviour.

Both bugs found while building this service were caught by tests written first:
a formatter reading the request id too late, and a `401` with no retry budget
surfacing as a rate-limit error. Neither was visible by reading the code.
