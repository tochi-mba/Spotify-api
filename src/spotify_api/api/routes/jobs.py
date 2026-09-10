"""Job inspection endpoints."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query, status

from spotify_api.api.dependencies import JobRunnerDep, JobStoreDep
from spotify_api.jobs.models import JobStatus
from spotify_api.models.jobs import JobListResponse, JobResponse
from spotify_api.models.responses import ErrorResponse

__all__ = ["router"]

router = APIRouter(prefix="/jobs", tags=["jobs"])

_NOT_FOUND: dict[int | str, dict[str, Any]] = {
    status.HTTP_404_NOT_FOUND: {
        "model": ErrorResponse,
        "description": "No such job, or it has aged out of the store.",
    }
}


@router.get(
    "",
    response_model=JobListResponse,
    summary="List recent jobs",
    description="Jobs this process is running or has recently run, newest first.",
)
async def list_jobs(
    store: JobStoreDep,
    job_status: Annotated[
        JobStatus | None, Query(alias="status", description="Only jobs in this state.")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200, description="Most jobs to return.")] = 50,
) -> JobListResponse:
    """Return recent jobs."""
    jobs = await store.list(status=job_status, limit=limit)
    return JobListResponse(count=len(jobs), jobs=[JobResponse.of(job) for job in jobs])


@router.get(
    "/{job_id}",
    response_model=JobResponse,
    summary="Get a job's status and result",
    description=(
        "Poll this after a call made with `?async=true`. While `status` is `pending` "
        "or `running` the work is still going; `succeeded` carries the body the "
        "synchronous call would have returned."
    ),
    responses=_NOT_FOUND,
)
async def get_job(job_id: str, store: JobStoreDep) -> JobResponse:
    """Return one job."""
    return JobResponse.of(await store.get(job_id))


@router.delete(
    "/{job_id}",
    response_model=JobResponse,
    summary="Cancel a running job",
    description=(
        "Stops a job that is still running. A job that has already finished is "
        "returned unchanged -- cancelling it would be a lie."
    ),
    responses=_NOT_FOUND,
)
async def cancel_job(job_id: str, runner: JobRunnerDep, store: JobStoreDep) -> JobResponse:
    """Cancel a job."""
    await runner.cancel(job_id)
    return JobResponse.of(await store.get(job_id))
