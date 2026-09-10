"""Running jobs in the background.

A submitted job becomes an ``asyncio.Task`` that outlives the request which
created it. Two things matter and are tested:

* the task holds a strong reference until it finishes, because asyncio only
  keeps weak references to running tasks and a garbage-collected task simply
  stops -- silently;
* finished tasks are dropped, because a long-lived process must not accumulate
  one Task object per job it has ever run.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from spotify_api.errors import ServiceError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from spotify_api.jobs.models import Job
    from spotify_api.jobs.protocols import JobStore

__all__ = ["JobRunner"]

_logger = logging.getLogger(__name__)

#: Shown when something we did not anticipate goes wrong. The detail stays in
#: the logs rather than being handed to a client.
_OPAQUE_ERROR = "an unexpected error occurred while running this job"


class JobRunner:
    """Submits work to the background and records what happens to it."""

    def __init__(self, *, store: JobStore) -> None:
        """Store the job store this runner writes outcomes to."""
        self.store = store
        self._tasks: dict[str, asyncio.Task[None]] = {}

    @property
    def in_flight(self) -> int:
        """How many jobs are currently running."""
        return len(self._tasks)

    async def submit(self, *, operation: str, work: Callable[[], Awaitable[Any]]) -> Job:
        """Accept ``work``, start it in the background, and return the job."""
        job = await self.store.create(operation=operation)
        task = asyncio.create_task(self._run(job.job_id, work), name=f"job:{operation}")
        # Held strongly until done: asyncio keeps only a weak reference, so an
        # unreferenced task can be collected mid-flight and vanish silently.
        self._tasks[job.job_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(job.job_id, None))
        return job

    async def cancel(self, job_id: str) -> None:
        """Cancel a running job. A finished job is left as it is."""
        job = await self.store.get(job_id)
        if job.is_terminal:
            return
        task = self._tasks.get(job_id)
        if task is not None:
            task.cancel()
        await self.store.cancel(job_id)

    async def shutdown(self) -> None:
        """Cancel everything still running, for a clean process exit."""
        for job_id, task in list(self._tasks.items()):
            task.cancel()
            await self.store.cancel(job_id)
        self._tasks.clear()

    async def _run(self, job_id: str, work: Callable[[], Awaitable[Any]]) -> None:
        """Execute one job, recording whatever happens."""
        await self.store.start(job_id)
        try:
            result = await work()
        except asyncio.CancelledError:
            _logger.info("job cancelled", extra={"job_id": job_id})
            raise
        except ServiceError as exc:
            _logger.warning("job failed", extra={"job_id": job_id, "error_type": exc.error_type})
            await self.store.fail(job_id, error=exc.message, error_type=exc.error_type)
        except Exception:
            _logger.exception("job failed unexpectedly", extra={"job_id": job_id})
            await self.store.fail(job_id, error=_OPAQUE_ERROR, error_type="internal_error")
        else:
            await self.store.finish(job_id, result=result)
