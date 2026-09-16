"""What a job is."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

__all__ = ["Job", "JobStatus"]


class JobStatus(StrEnum):
    """Where a job has got to."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


#: Statuses a job will never leave.
TERMINAL = frozenset({JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED})


class Job(BaseModel):
    """One backgrounded operation and its outcome."""

    job_id: str = Field(description="Identifier to poll with.")
    operation: str = Field(description="What was asked for, e.g. 'player.play'.")
    account_id: str = Field(
        description=(
            "The keyring account whose verified token created this job. Only that account may "
            "read or cancel it, and it is never rendered into a response."
        )
    )
    status: JobStatus = Field(description="pending, running, succeeded, failed or cancelled.")
    created_at: float = Field(description="Monotonic timestamp the job was accepted.")
    started_at: float | None = Field(default=None, description="When work began.")
    completed_at: float | None = Field(default=None, description="When the outcome was known.")
    result: Any = Field(
        default=None, description="The response body the synchronous call would have returned."
    )
    error: str | None = Field(default=None, description="Why it failed, when it did.")
    error_type: str | None = Field(
        default=None, description="Machine-readable discriminator for the failure."
    )
    attempts: int = Field(
        default=0, description="How many times the outcome was polled for before it was known."
    )

    @property
    def is_terminal(self) -> bool:
        """Whether this job has reached a state it will never leave."""
        return self.status in TERMINAL
