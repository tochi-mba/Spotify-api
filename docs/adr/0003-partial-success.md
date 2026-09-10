# ADR 0003 — Answer batches with partial success

**Status:** accepted

## Context

A lookup request carries up to fifty independent items. Some will resolve, some
will not match anything, and some may fail because Spotify rate-limited that
particular call. We must decide what the response looks like when a batch is
mixed.

## Decision

Return `200` with one result per submitted item, each carrying its own
`found` / `not_found` / `error` status. Reserve non-2xx for failures of the
*request*: a malformed body, or an upstream so unavailable that nothing could be
attempted.

## Rationale

- The alternative — failing the batch on the first bad item — throws away
  forty-nine successful lookups because of one, and forces the caller to retry
  work that already succeeded.
- "Not found" is a legitimate answer, not an error. A caller submitting messy
  data expects some misses and should not have to distinguish them from
  failures by parsing an error message.
- Echoing `query` alongside each result means the caller can line results up
  with their input without tracking indices themselves, though `index` is there
  too.

## Consequences

- Callers **must** inspect per-item `status`; a `200` does not mean everything
  resolved. This is called out first in the README and the API reference.
- The resolver must catch every exception per item — including ones we did not
  anticipate — or a single failure escapes and fails the gather. That is
  invariant 2.1 in AGENTS.md and is tested explicitly.
- Unexpected exceptions become an opaque message. Internal detail goes to the
  logs; leaking it into a per-item `error` string would put stack-trace
  fragments in a client-facing field.
- `count` is computed from `results` rather than supplied, so it can never
  disagree with them.
