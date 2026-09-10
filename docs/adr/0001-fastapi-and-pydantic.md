# ADR 0001 — Use FastAPI and Pydantic v2

**Status:** accepted

## Context

The service's whole job is accepting a structured JSON body, validating it hard,
and returning a structured JSON body. Whatever framework we choose will mostly
be doing schema work, and the result is meant to be consumed by other software —
an MCP server, a batch job — which makes a machine-readable schema valuable
rather than decorative.

## Decision

FastAPI with Pydantic v2, served by uvicorn.

## Rationale

- Validation is declarative and lives in the models, so the contract is enforced
  in exactly one place instead of being re-checked in each handler.
- OpenAPI is generated from those same models, so the published schema cannot
  drift from the code that enforces it. A consumer can generate a client.
- Async is native, which matters because a batch is N concurrent upstream calls;
  a sync framework would need a thread pool to do the same job.
- Dependency injection with `dependency_overrides` gives clean test seams
  without patching.

## Consequences

- Route signatures are evaluated at runtime, so their imports cannot move into
  `TYPE_CHECKING` blocks. Ruff's `TC001`/`TC002` are disabled for `api/` with a
  comment explaining why.
- Pydantic models are the public contract. Changing one is an API change and
  should be treated as such.
- Rejected: Flask (hand-wired validation, no OpenAPI, sync upstream calls) and
  Litestar (equivalent, smaller ecosystem, fewer people know it).
