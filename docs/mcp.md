# Fronting this as MCP tools

Nothing MCP-specific is implemented. The HTTP surface is already the shape a wrapper
needs: summaries and descriptions written for a caller that is often a model, partial
success on lookup, and jobs that a tool can wait on instead of polling.

Assign stable `operation_id`s before wrapping. Most OpenAPI-to-MCP bridges name tools
from those ids, and renaming one later is a breaking change for every bound client.

## The tools, and when to call each

| Tool | When |
| --- | --- |
| `lookup` | Resolve a batch of loosely-specified tracks. One item failing never fails the batch. |
| Player reads (`devices`, state, queue, recently played) | Before commanding playback, so a missing device is a named 409 rather than a guessed id. |
| Player writes (`play`, `pause`, `next`, `previous`, seek, volume, shuffle, repeat, transfer, queue) | Only after there is an active device. Premium is required; a 403 says so. |
| `GET /v1/jobs/{job_id}` | After `?async=true`. Pass `wait_seconds` (0–60) instead of looping. |

A playback `204` from Spotify means the command was accepted, not that audio is playing.
The job is not finished until the effect is confirmed on the device. The wrapper should
say that in the tool description, because a model that treats `202` as "it is playing"
will immediately ask why it is not.

## What a model must never see

This service holds no Spotify credential. After the token is checked it asks keyring for
the headers to attach, and those headers never enter a response, a job result, or a log.
A wrapper that echoed a `ResolvedAuth` into a tool result would undo that.

Another account's job is 404, identical to one that never existed. Do not paper over that
with a 403: a 403 confirms the job exists.

## Wrapping it

FastMCP pointed at `/openapi.json` with an **allowlist** of lookup, player, and jobs.
A denylist means the next endpoint is exposed by default.

`wait_seconds` is the shape a tool actually wants. A bridge that strips query parameters
and forces the model to poll will spend tokens on `GET` loops this service already
collapsed into one call.

## Known gaps

**No stable `operation_id`s yet.** Add them, pin them with a contract test, then wrap.
Until then a generated tool name is a path, and a path is not a public API.

**No `Idempotency-Key` on writes.** Assistants retry. Playback commands are not
duplicated by the job store; a retried `?async=true` still starts a second job.
