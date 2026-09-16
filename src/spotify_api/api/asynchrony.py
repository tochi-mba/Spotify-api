"""The ``?async=true`` mechanism.

Every endpoint takes the same flag and behaves the same way under it, so a
caller learns one interaction pattern rather than one per resource:

* ``async=false`` (the default) -- do the work, answer with the result.
* ``async=true`` -- accept the work, answer ``202`` with a job id, and let the
  caller poll ``/v1/jobs/{id}``.

The work itself is written once, as a coroutine, and is identical either way.
That is the point: there is no separate async code path to keep in step.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, TypeVar

from fastapi import Query
from fastapi.responses import JSONResponse
from starlette import status

from spotify_api.models.jobs import JobAccepted

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from spotify_api.jobs.runner import JobRunner

__all__ = ["AsyncFlag", "run_or_submit"]

#: Whatever the endpoint itself returns when run synchronously.
T = TypeVar("T")

#: The flag itself, declared once so every route documents it identically.
AsyncFlag = Annotated[
    bool,
    Query(
        alias="async",
        description=(
            "Run in the background. Answers 202 with a job id immediately; poll "
            "/v1/jobs/{job_id} for the outcome. For playback commands the job is not "
            "finished until the effect has been confirmed on the device -- not merely "
            "accepted by Spotify."
        ),
    ),
]


async def run_or_submit(
    *,
    run_async: bool,
    operation: str,
    account_id: str,
    work: Callable[[], Awaitable[T]],
    runner: JobRunner,
) -> T | JSONResponse:
    """Either do the work now, or hand it to the background and answer 202.

    Args:
        run_async: The caller's ``?async=`` flag.
        operation: Dotted name recorded on the job, e.g. ``"player.play"``.
        account_id: The verified account the job belongs to. Only it may read or cancel it.
        work: The coroutine to run. Identical in both modes.
        runner: Where background work is submitted.

    Returns:
        The work's own result synchronously, or a ``202`` carrying a job id.
    """
    if not run_async:
        return await work()

    job = await runner.submit(operation=operation, account_id=account_id, work=work)
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content=JobAccepted.of(job).model_dump(mode="json"),
        headers={"Location": f"/v1/jobs/{job.job_id}"},
    )
