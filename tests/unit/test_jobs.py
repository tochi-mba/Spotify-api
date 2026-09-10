"""Jobs run work in the background and report a confirmed outcome."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from spotify_api.errors import JobNotFoundError, NoActiveDeviceError, SpotifyUnavailableError
from spotify_api.jobs.models import JobStatus
from spotify_api.jobs.protocols import JobStore
from spotify_api.jobs.runner import JobRunner
from spotify_api.jobs.store import InMemoryJobStore


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store(clock: FakeClock) -> InMemoryJobStore:
    return InMemoryJobStore(clock=clock, ttl_seconds=3600)


@pytest.fixture
def runner(store: InMemoryJobStore) -> JobRunner:
    return JobRunner(store=store)


async def settle() -> None:
    """Let background tasks run to completion."""
    for _ in range(20):
        await asyncio.sleep(0)


# -- the store --------------------------------------------------------------


async def test_a_created_job_starts_pending(store: InMemoryJobStore) -> None:
    job = await store.create(operation="player.play")

    assert job.status is JobStatus.PENDING
    assert job.operation == "player.play"
    assert job.result is None
    assert job.error is None
    assert job.job_id


async def test_job_ids_are_unique(store: InMemoryJobStore) -> None:
    ids = {(await store.create(operation="op")).job_id for _ in range(50)}
    assert len(ids) == 50


async def test_a_job_can_be_read_back(store: InMemoryJobStore) -> None:
    created = await store.create(operation="op")
    assert (await store.get(created.job_id)).job_id == created.job_id


async def test_reading_an_unknown_job_is_an_error(store: InMemoryJobStore) -> None:
    with pytest.raises(JobNotFoundError, match="no such job"):
        await store.get("does-not-exist")


async def test_jobs_are_listed_newest_first(store: InMemoryJobStore, clock: FakeClock) -> None:
    first = await store.create(operation="a")
    clock.advance(1)
    second = await store.create(operation="b")

    assert [job.job_id for job in await store.list()] == [second.job_id, first.job_id]


async def test_listing_can_be_filtered_by_status(store: InMemoryJobStore) -> None:
    done = await store.create(operation="a")
    await store.finish(done.job_id, result={"ok": True})
    await store.create(operation="b")

    listed = await store.list(status=JobStatus.SUCCEEDED)
    assert [job.job_id for job in listed] == [done.job_id]


async def test_listing_is_bounded(store: InMemoryJobStore) -> None:
    for _ in range(10):
        await store.create(operation="op")
    assert len(await store.list(limit=3)) == 3


async def test_finishing_a_job_records_its_result(store: InMemoryJobStore) -> None:
    job = await store.create(operation="op")
    await store.finish(job.job_id, result={"is_playing": True})

    stored = await store.get(job.job_id)
    assert stored.status is JobStatus.SUCCEEDED
    assert stored.result == {"is_playing": True}
    assert stored.completed_at is not None


async def test_failing_a_job_records_why(store: InMemoryJobStore) -> None:
    job = await store.create(operation="op")
    await store.fail(job.job_id, error="no active device", error_type="no_active_device")

    stored = await store.get(job.job_id)
    assert stored.status is JobStatus.FAILED
    assert stored.error == "no active device"
    assert stored.error_type == "no_active_device"


async def test_expired_jobs_are_reaped(store: InMemoryJobStore, clock: FakeClock) -> None:
    job = await store.create(operation="op")
    clock.advance(3601)
    with pytest.raises(JobNotFoundError):
        await store.get(job.job_id)


async def test_an_unexpired_job_survives(store: InMemoryJobStore, clock: FakeClock) -> None:
    job = await store.create(operation="op")
    clock.advance(3599)
    assert await store.get(job.job_id)


async def test_reaping_removes_expired_jobs_from_listings(
    store: InMemoryJobStore, clock: FakeClock
) -> None:
    await store.create(operation="old")
    clock.advance(3601)
    fresh = await store.create(operation="new")
    assert [job.operation for job in await store.list()] == ["new"]
    assert fresh.job_id


async def test_updating_an_unknown_job_is_an_error(store: InMemoryJobStore) -> None:
    with pytest.raises(JobNotFoundError):
        await store.finish("nope", result={})
    with pytest.raises(JobNotFoundError):
        await store.fail("nope", error="x", error_type="y")


# -- the runner -------------------------------------------------------------


async def test_a_submitted_job_runs_and_records_its_result(runner: JobRunner) -> None:
    async def work() -> dict[str, Any]:
        return {"ok": True}

    job = await runner.submit(operation="op", work=work)
    assert job.status is JobStatus.PENDING

    await settle()
    assert (await runner.store.get(job.job_id)).status is JobStatus.SUCCEEDED
    assert (await runner.store.get(job.job_id)).result == {"ok": True}


async def test_the_caller_is_not_blocked_while_the_job_runs(runner: JobRunner) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def work() -> dict[str, Any]:
        started.set()
        await release.wait()
        return {"ok": True}

    job = await runner.submit(operation="op", work=work)
    await asyncio.wait_for(started.wait(), timeout=1)
    assert (await runner.store.get(job.job_id)).status is JobStatus.RUNNING

    release.set()
    await settle()
    assert (await runner.store.get(job.job_id)).status is JobStatus.SUCCEEDED


async def test_a_declared_failure_is_recorded_with_its_type(runner: JobRunner) -> None:
    async def work() -> dict[str, Any]:
        raise NoActiveDeviceError("no active Spotify device was found")

    job = await runner.submit(operation="player.play", work=work)
    await settle()

    stored = await runner.store.get(job.job_id)
    assert stored.status is JobStatus.FAILED
    assert stored.error_type == "no_active_device"
    assert "no active Spotify device" in (stored.error or "")


async def test_an_unexpected_failure_is_contained_and_not_leaked(runner: JobRunner) -> None:
    async def work() -> dict[str, Any]:
        raise RuntimeError("connection pool corrupted")

    job = await runner.submit(operation="op", work=work)
    await settle()

    stored = await runner.store.get(job.job_id)
    assert stored.status is JobStatus.FAILED
    assert stored.error_type == "internal_error"
    assert "connection pool corrupted" not in (stored.error or "")


async def test_one_failing_job_does_not_affect_another(runner: JobRunner) -> None:
    async def boom() -> dict[str, Any]:
        raise SpotifyUnavailableError("upstream down")

    async def fine() -> dict[str, Any]:
        return {"ok": True}

    bad = await runner.submit(operation="a", work=boom)
    good = await runner.submit(operation="b", work=fine)
    await settle()

    assert (await runner.store.get(bad.job_id)).status is JobStatus.FAILED
    assert (await runner.store.get(good.job_id)).status is JobStatus.SUCCEEDED


async def test_a_job_can_be_cancelled_while_it_runs(runner: JobRunner) -> None:
    started = asyncio.Event()

    async def work() -> dict[str, Any]:
        started.set()
        await asyncio.sleep(60)
        return {}

    job = await runner.submit(operation="op", work=work)
    await asyncio.wait_for(started.wait(), timeout=1)

    await runner.cancel(job.job_id)
    await settle()

    assert (await runner.store.get(job.job_id)).status is JobStatus.CANCELLED


async def test_cancelling_a_finished_job_leaves_it_alone(runner: JobRunner) -> None:
    async def work() -> dict[str, Any]:
        return {"ok": True}

    job = await runner.submit(operation="op", work=work)
    await settle()
    await runner.cancel(job.job_id)

    assert (await runner.store.get(job.job_id)).status is JobStatus.SUCCEEDED


async def test_cancelling_an_unknown_job_is_an_error(runner: JobRunner) -> None:
    with pytest.raises(JobNotFoundError):
        await runner.cancel("nope")


async def test_shutdown_cancels_everything_still_running(runner: JobRunner) -> None:
    async def work() -> dict[str, Any]:
        await asyncio.sleep(60)
        return {}

    job = await runner.submit(operation="op", work=work)
    await settle()
    await runner.shutdown()

    assert (await runner.store.get(job.job_id)).status is JobStatus.CANCELLED


async def test_shutdown_is_safe_with_nothing_running(runner: JobRunner) -> None:
    await runner.shutdown()


async def test_finished_tasks_are_not_retained(runner: JobRunner) -> None:
    # A long-lived process must not accumulate one Task object per job ever run.
    async def work() -> dict[str, Any]:
        return {}

    for _ in range(5):
        await runner.submit(operation="op", work=work)
    await settle()

    assert runner.in_flight == 0


async def test_recording_an_attempt_against_an_unknown_job_is_harmless(
    store: InMemoryJobStore,
) -> None:
    # The confirmer records attempts while polling; a job reaped mid-wait must
    # not turn that bookkeeping into a crash.
    await store.record_attempt("never-existed")


async def test_attempts_accumulate(store: InMemoryJobStore) -> None:
    job = await store.create(operation="op")
    await store.record_attempt(job.job_id)
    await store.record_attempt(job.job_id)
    assert (await store.get(job.job_id)).attempts == 2


async def test_cancelling_a_job_this_runner_never_started_still_records_it(
    runner: JobRunner, store: InMemoryJobStore
) -> None:
    # Stores outlive runners; a job with no task here is still cancellable.
    job = await store.create(operation="orphan")
    await runner.cancel(job.job_id)
    assert (await store.get(job.job_id)).status is JobStatus.CANCELLED


def test_the_in_memory_store_satisfies_the_seam(store: InMemoryJobStore) -> None:
    assert isinstance(store, JobStore)
