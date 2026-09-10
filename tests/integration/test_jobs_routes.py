"""Jobs are inspectable, cancellable, and reachable the same way from any route."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from spotify_api.errors import NoActiveDeviceError

if TYPE_CHECKING:
    import httpx

    from tests.integration.conftest import FakeResolver


async def settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


async def submit(client: httpx.AsyncClient, **overrides: Any) -> dict[str, Any]:
    """Start an async lookup and return the 202 body."""
    payload = {"items": [{"name": "x"}]}
    payload.update(overrides)
    response = await client.post("/v1/lookup?async=true", json=payload)
    assert response.status_code == 202
    body: dict[str, Any] = response.json()
    return body


async def test_an_async_call_answers_202_with_a_job_to_poll(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post("/v1/lookup?async=true", json={"items": [{"name": "x"}]})

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "pending"
    assert body["operation"] == "lookup"
    assert body["poll_url"] == f"/v1/jobs/{body['job_id']}"
    assert response.headers["Location"] == body["poll_url"]


async def test_the_default_is_still_synchronous(client: httpx.AsyncClient) -> None:
    response = await client.post("/v1/lookup", json={"items": [{"name": "x"}]})
    assert response.status_code == 200
    assert "results" in response.json()


async def test_async_false_is_synchronous(client: httpx.AsyncClient) -> None:
    response = await client.post("/v1/lookup?async=false", json={"items": [{"name": "x"}]})
    assert response.status_code == 200


async def test_polling_returns_the_result_the_sync_call_would_have_given(
    client: httpx.AsyncClient,
) -> None:
    accepted = await submit(client)
    await settle()

    response = await client.get(accepted["poll_url"])
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "succeeded"
    assert body["result"]["count"] == 1
    assert body["result"]["results"][0]["status"] == "not_found"
    assert body["error"] is None


async def test_a_failing_job_records_why(client: httpx.AsyncClient, resolver: FakeResolver) -> None:
    resolver.raises = NoActiveDeviceError("no active Spotify device was found")
    accepted = await submit(client)
    await settle()

    body = (await client.get(accepted["poll_url"])).json()
    assert body["status"] == "failed"
    assert body["error_type"] == "no_active_device"
    assert "no active Spotify device" in body["error"]


async def test_an_unexpected_failure_does_not_leak_its_detail(
    client: httpx.AsyncClient, resolver: FakeResolver
) -> None:
    resolver.raises = RuntimeError("connection pool corrupted")
    accepted = await submit(client)
    await settle()

    body = (await client.get(accepted["poll_url"])).json()
    assert body["status"] == "failed"
    assert body["error_type"] == "internal_error"
    assert "connection pool corrupted" not in (await client.get(accepted["poll_url"])).text


async def test_an_unknown_job_is_a_404_with_the_error_envelope(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/v1/jobs/does-not-exist")

    assert response.status_code == 404
    error = response.json()["error"]
    assert error["type"] == "job_not_found"


async def test_jobs_can_be_listed(client: httpx.AsyncClient) -> None:
    await submit(client)
    await submit(client)
    await settle()

    body = (await client.get("/v1/jobs")).json()
    assert body["count"] == 2
    assert {job["operation"] for job in body["jobs"]} == {"lookup"}


async def test_listing_can_be_filtered_and_limited(client: httpx.AsyncClient) -> None:
    await submit(client)
    await submit(client)
    await settle()

    succeeded = (await client.get("/v1/jobs?status=succeeded")).json()
    assert succeeded["count"] == 2

    limited = (await client.get("/v1/jobs?limit=1")).json()
    assert limited["count"] == 1

    cancelled = (await client.get("/v1/jobs?status=cancelled")).json()
    assert cancelled["count"] == 0


async def test_an_invalid_status_filter_is_rejected(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/jobs?status=nonsense")
    assert response.status_code == 422
    assert response.json()["error"]["type"] == "validation_error"


async def test_a_running_job_can_be_cancelled(
    client: httpx.AsyncClient, resolver: FakeResolver
) -> None:
    release = asyncio.Event()
    resolver.gate = release

    accepted = await submit(client)
    await settle()
    assert (await client.get(accepted["poll_url"])).json()["status"] == "running"

    response = await client.delete(accepted["poll_url"])
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"

    release.set()
    await settle()
    assert (await client.get(accepted["poll_url"])).json()["status"] == "cancelled"


async def test_cancelling_a_finished_job_leaves_its_result_alone(
    client: httpx.AsyncClient,
) -> None:
    accepted = await submit(client)
    await settle()

    response = await client.delete(accepted["poll_url"])
    assert response.json()["status"] == "succeeded"


async def test_cancelling_an_unknown_job_is_a_404(client: httpx.AsyncClient) -> None:
    assert (await client.delete("/v1/jobs/nope")).status_code == 404


async def test_a_job_reports_how_long_the_work_took(client: httpx.AsyncClient) -> None:
    accepted = await submit(client)
    await settle()

    body = (await client.get(accepted["poll_url"])).json()
    assert body["duration_seconds"] is not None
    assert body["duration_seconds"] >= 0


async def test_an_async_call_still_validates_its_body_first(
    client: httpx.AsyncClient, resolver: FakeResolver
) -> None:
    # A malformed request must fail now, not become a job that fails later.
    response = await client.post("/v1/lookup?async=true", json={"items": []})

    assert response.status_code == 422
    assert resolver.batches == []


async def test_an_async_call_still_requires_a_user_token(
    anonymous_client: httpx.AsyncClient,
) -> None:
    response = await anonymous_client.post("/v1/lookup?async=true", json={"items": [{"name": "x"}]})
    assert response.status_code == 401
