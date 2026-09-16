# Architecture decision records

One file per decision that would otherwise be re-argued every few months. Each one says
what was decided, what it cost, and what would make it worth revisiting. They are dated
records rather than documentation: when a decision is reversed, the record stays and the
one that replaced it says so.

| ADR | Decision | Status |
| --- | --- | --- |
| [0001](0001-fastapi-and-pydantic.md) | FastAPI and pydantic, with the schema as the contract | Accepted |
| [0002](0002-client-credentials.md) | Spotify credentials come from the client-credentials flow | **Superseded** — this service now holds no Spotify credential at all. Each caller's keyring token is verified locally and keyring supplies the headers, per request, per person. See [docs/keyring.md](../keyring.md). |
| [0003](0003-partial-success.md) | A batch reports per-item outcomes rather than failing whole | Accepted |
| [0004](0004-full-coverage-gate.md) | 100% branch coverage is a gate, not a target | Accepted |

## Writing one

Copy the shape of an existing record: context, decision, consequences, and what would
change the answer. Number it in sequence. A decision that only affects this repository
lives here; one that binds the whole family lives in the meta repository's `docs/adr/`
and is linked from here rather than restated.
