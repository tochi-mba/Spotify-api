# Contributing

Start with [AGENTS.md](AGENTS.md) — it is the operating manual, and it is
normative. This file is the short version.

## Setup

```bash
uv sync --all-extras
uv run pre-commit install
cp .env.example .env     # add credentials only if you want to run `make live`
```

The full test suite needs no credentials and no network.

## The loop

1. **Write a failing test.** Run it. Confirm it fails for the reason you expect.
2. Write the smallest code that makes it pass.
3. Refactor with it green.
4. `make check` — ruff, format, mypy `--strict`, pytest at 100% branch coverage.

All four must pass before you commit. Beware piping to `tail` in a shell chain:
`tail`'s exit code hides the failure. `make check` does not have that problem.

## Commits

Conventional prefixes (`feat:`, `fix:`, `test:`, `docs:`, `chore:`,
`refactor:`). The subject says what changed; the body says **why**, and flags
anything surprising — a bug found, a trade-off taken, something deliberately not
done. One logical change per commit.

## Pull requests

- Describe the behaviour change, not the diff.
- Note any new configuration and confirm it is in the README table and
  `.env.example`.
- Confirm `make check` passes locally; CI runs the same thing on 3.11, 3.12 and
  3.13.

## Things that will get a change sent back

- Coverage lowered, or `# pragma: no cover` used to dodge the gate.
- A route importing `SpotifyClient` instead of depending on `TrackResolver`.
- Validation rules in a handler that belong in a model.
- An exception escaping per-item resolution and failing a whole batch.
- A secret that can reach a log line or a response body.
- `time.time()` or `asyncio.sleep()` called directly in adapter code instead of
  going through the injected `clock` / `sleeper`.
