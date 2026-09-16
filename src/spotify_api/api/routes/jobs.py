"""Job inspection endpoints.

A job belongs to the account whose verified token started it, and every route here answers
only for the caller's own account. Another person's job is a 404 identical to one that never
existed: a job's result is the body the synchronous call would have returned -- somebody's
playback state, somebody's lookups -- so "exists but is not yours" would confirm another
person's activity.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query, status

from spotify_api.api.dependencies import JobRunnerDep, JobStoreDep, UserContextDep
from spotify_api.jobs.models import JobStatus
from spotify_api.models.jobs import JobListResponse, JobResponse
from spotify_api.models.responses import Problem

__all__ = ["router"]

router = APIRouter(prefix="/jobs", tags=["jobs"])

_UNAUTHORIZED: dict[int | str, dict[str, Any]] = {
    status.HTTP_401_UNAUTHORIZED: {
        "model": Problem,
        "description": "The keyring user token is missing or was refused.",
    },
    status.HTTP_503_SERVICE_UNAVAILABLE: {
        "model": Problem,
        "description": "keyring's signing keys could not be fetched to check the token.",
    },
}

_NOT_FOUND: dict[int | str, dict[str, Any]] = {
    **_UNAUTHORIZED,
    status.HTTP_404_NOT_FOUND: {
        "model": Problem,
        "description": "No such job for this account, or it has aged out of the store.",
    },
}


@router.get(
    "",
    response_model=JobListResponse,
    summary="List recent jobs",
    description="Your jobs that this process is running or has recently run, newest first.",
    responses=_UNAUTHORIZED,
)
async def list_jobs(
    store: JobStoreDep,
    context: UserContextDep,
    job_status: Annotated[
        JobStatus | None, Query(alias="status", description="Only jobs in this state.")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200, description="Most jobs to return.")] = 50,
) -> JobListResponse:
    """Return the caller's recent jobs."""
    jobs = await store.list(account_id=context.account_id, status=job_status, limit=limit)
    return JobListResponse(count=len(jobs), jobs=[JobResponse.of(job) for job in jobs])


@router.get(
    "/{job_id}",
    response_model=JobResponse,
    summary="Get a job's status and result",
    description=(
        "Poll this after a call made with `?async=true`. While `status` is `pending` "
        "or `running` the work is still going; `succeeded` carries the body the "
        "synchronous call would have returned. Only the account that started the job "
        "can see it."
    ),
    responses=_NOT_FOUND,
)
async def get_job(job_id: str, store: JobStoreDep, context: UserContextDep) -> JobResponse:
    """Return one of the caller's jobs."""
    return JobResponse.of(await store.get(job_id, account_id=context.account_id))


@router.delete(
    "/{job_id}",
    response_model=JobResponse,
    summary="Cancel a running job",
    description=(
        "Stops a job that is still running. A job that has already finished is "
        "returned unchanged -- cancelling it would be a lie. Only the account that "
        "started the job can cancel it."
    ),
    responses=_NOT_FOUND,
)
async def cancel_job(
    job_id: str, runner: JobRunnerDep, store: JobStoreDep, context: UserContextDep
) -> JobResponse:
    """Cancel one of the caller's jobs."""
    await runner.cancel(job_id, account_id=context.account_id)
    return JobResponse.of(await store.get(job_id, account_id=context.account_id))
