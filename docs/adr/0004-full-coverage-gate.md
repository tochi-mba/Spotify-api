# ADR 0004 — Enforce 100% branch coverage in CI

**Status:** accepted

## Context

The project was specified as fully test-driven with complete coverage. Coverage
targets are usually set at 80–90% precisely because 100% is thought to invite
box-ticking tests written to satisfy a number.

## Decision

`--cov-fail-under=100` with `--cov-branch`, wired into `addopts` from the very
first commit, before any feature code existed.

## Rationale

- Enabling it first means it was never retrofitted, so there was never a moment
  where lowering it was the convenient option.
- Branch coverage, not line coverage. Line coverage would call an `if` with no
  tested `else` fully covered, which is where bugs actually live.
- In practice every gap this gate surfaced was a real case worth a test:
  explicit JSON nulls (a client that emits every key sends `"artist": null`), a
  JSON body that is valid but not an object, a token refresh with no retry
  budget, and the readiness check. None was a box-ticking test.

## Consequences

- The exclusion list is fixed and lives in `pyproject.toml`: `TYPE_CHECKING`
  blocks, protocol bodies, `NotImplementedError`, and the `__main__` guard.
  Adding to it requires a reason in the commit message.
- `# pragma: no cover` must never be used to dodge the gate. If a line is truly
  unreachable, delete it.
- Some code is shaped for testability: time and sleep are injected rather than
  called directly, which is why the retry ladder can be asserted in
  milliseconds instead of waited out.
- The honest cost: a genuinely untestable third-party edge case would force
  either an exclusion or a redesign. Nothing here has hit that yet.
