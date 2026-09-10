"""In-memory job storage.

Jobs live in a dict guarded by a lock, with a TTL so a long-running process
does not accumulate every job it has ever run.

The honest limitation: jobs vanish on restart, and a second replica knows
nothing of the first's jobs. That is acceptable because a job's lifetime is
seconds and the caller is polling right now -- and because the ``JobStore``
protocol means swapping in Redis is one new class, not a rewrite. See
docs/adr/0007-in-memory-job-store.md.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import TYPE_CHECKING, Any

from spotify_api.errors import JobNotFoundError
from spotify_api.jobs.models import Job, JobStatus

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = ["InMemoryJobStore"]

#: How long a finished job stays readable before it is reaped.
DEFAULT_TTL_SECONDS = 3600


class InMemoryJobStore:
    """Holds jobs in this process's memory."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
    ) -> None:
        """Store the clock and how long jobs are kept."""
        self._clock = clock
        self._ttl = ttl_seconds
        self._jobs: dict[str, Job] = {}
        self._lock = asyncio.Lock()

    async def create(self, *, operation: str) -> Job:
        """Record a new pending job and return it."""
        job = Job(
            job_id=str(uuid.uuid4()),
            operation=operation,
            status=JobStatus.PENDING,
            created_at=self._clock(),
        )
        async with self._lock:
            self._reap()
            self._jobs[job.job_id] = job
        return job

    async def get(self, job_id: str) -> Job:
        """Return one job.

        Raises:
            JobNotFoundError: the job is unknown, or has aged out.
        """
        async with self._lock:
            self._reap()
            job = self._jobs.get(job_id)
        if job is None:
            message = "no such job; it may have expired"
            raise JobNotFoundError(message, job_id=job_id)
        return job

    async def list(self, *, status: JobStatus | None = None, limit: int = 50) -> list[Job]:
        """Return jobs, newest first."""
        async with self._lock:
            self._reap()
            jobs = list(self._jobs.values())
        if status is not None:
            jobs = [job for job in jobs if job.status is status]
        jobs.sort(key=lambda job: job.created_at, reverse=True)
        return jobs[:limit]

    async def start(self, job_id: str) -> None:
        """Mark a job as running."""
        await self._update(job_id, status=JobStatus.RUNNING, started_at=self._clock())

    async def finish(self, job_id: str, *, result: Any) -> None:  # noqa: ANN401
        """Record a successful outcome."""
        await self._update(
            job_id, status=JobStatus.SUCCEEDED, result=result, completed_at=self._clock()
        )

    async def fail(self, job_id: str, *, error: str, error_type: str) -> None:
        """Record a failure."""
        await self._update(
            job_id,
            status=JobStatus.FAILED,
            error=error,
            error_type=error_type,
            completed_at=self._clock(),
        )

    async def cancel(self, job_id: str) -> None:
        """Record that a job was cancelled."""
        await self._update(job_id, status=JobStatus.CANCELLED, completed_at=self._clock())

    async def record_attempt(self, job_id: str) -> None:
        """Note that the outcome was polled for once more."""
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                # The job was reaped while the confirmer was still polling it.
                # Bookkeeping is not worth an exception.
                return
            self._jobs[job_id] = job.model_copy(update={"attempts": job.attempts + 1})

    # -- internals ----------------------------------------------------------

    async def _update(self, job_id: str, **changes: Any) -> None:  # noqa: ANN401
        """Replace a job with an updated copy."""
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                message = "no such job; it may have expired"
                raise JobNotFoundError(message, job_id=job_id)
            self._jobs[job_id] = job.model_copy(update=changes)

    def _reap(self) -> None:
        """Drop jobs older than the TTL. Called under the lock."""
        cutoff = self._clock() - self._ttl
        expired = [key for key, job in self._jobs.items() if job.created_at <= cutoff]
        for key in expired:
            del self._jobs[key]
