"""The job-store seam.

Routes and the runner depend on this, never on the dict behind it, so a durable
store (Redis, a database) can replace the in-memory one without touching either.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from spotify_api.jobs.models import Job, JobStatus

__all__ = ["JobStore"]


@runtime_checkable
class JobStore(Protocol):
    """Holds jobs and their outcomes."""

    async def create(self, *, operation: str) -> Job:
        """Record a new pending job and return it."""
        ...

    async def get(self, job_id: str) -> Job:
        """Return one job, raising if it is unknown or expired."""
        ...

    async def list(self, *, status: JobStatus | None = None, limit: int = 50) -> list[Job]:
        """Return jobs, newest first."""
        ...

    async def start(self, job_id: str) -> None:
        """Mark a job as running."""
        ...

    async def finish(self, job_id: str, *, result: Any) -> None:  # noqa: ANN401
        """Record a successful outcome."""
        ...

    async def fail(self, job_id: str, *, error: str, error_type: str) -> None:
        """Record a failure."""
        ...

    async def cancel(self, job_id: str) -> None:
        """Record that a job was cancelled."""
        ...

    async def record_attempt(self, job_id: str) -> None:
        """Note that the outcome was polled for once more."""
        ...
