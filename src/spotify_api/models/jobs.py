"""The public shape of a job."""

from __future__ import annotations

from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field

from spotify_api.jobs.models import Job, JobStatus

__all__ = ["JobAccepted", "JobListResponse", "JobResponse"]


class JobAccepted(BaseModel):
    """The answer to a call made with ``?async=true``."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "job_id": "0f8c1e2a-4b5d-4e6f-8a9b-0c1d2e3f4a5b",
                    "status": "pending",
                    "operation": "player.play",
                    "poll_url": "/v1/jobs/0f8c1e2a-4b5d-4e6f-8a9b-0c1d2e3f4a5b",
                }
            ]
        }
    )

    job_id: str = Field(description="Identifier to poll with.")
    status: JobStatus = Field(description="Always 'pending' at this point.")
    operation: str = Field(description="What was asked for.")
    poll_url: str = Field(description="Where to ask how it is going.")

    @classmethod
    def of(cls, job: Job) -> Self:
        """Build the acceptance answer for ``job``."""
        return cls(
            job_id=job.job_id,
            status=job.status,
            operation=job.operation,
            poll_url=f"/v1/jobs/{job.job_id}",
        )


class JobResponse(BaseModel):
    """A job and, once it has one, its outcome."""

    job_id: str = Field(description="Identifier this job was polled with.")
    operation: str = Field(description="What was asked for.")
    status: JobStatus = Field(description="pending, running, succeeded, failed or cancelled.")
    result: Any = Field(
        default=None,
        description=(
            "The body the synchronous call would have returned. Populated only when "
            "status is 'succeeded'."
        ),
    )
    error: str | None = Field(default=None, description="Why it failed, when it did.")
    error_type: str | None = Field(
        default=None, description="Machine-readable discriminator for the failure."
    )
    attempts: int = Field(
        description="How many times the outcome was polled for before it was known."
    )
    duration_seconds: float | None = Field(
        default=None, description="How long the work took, once it is finished."
    )

    @classmethod
    def of(cls, job: Job) -> Self:
        """Render ``job`` for a caller."""
        duration = (
            job.completed_at - job.started_at
            if job.completed_at is not None and job.started_at is not None
            else None
        )
        return cls(
            job_id=job.job_id,
            operation=job.operation,
            status=job.status,
            result=job.result,
            error=job.error,
            error_type=job.error_type,
            attempts=job.attempts,
            duration_seconds=round(duration, 3) if duration is not None else None,
        )


class JobListResponse(BaseModel):
    """A page of jobs, newest first."""

    count: int = Field(description="How many jobs are in this answer.")
    jobs: list[JobResponse] = Field(description="The jobs, newest first.")
