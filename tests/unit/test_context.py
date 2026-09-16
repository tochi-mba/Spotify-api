"""The request id and the verified account are task-local, and never leak between requests."""

from __future__ import annotations

import asyncio
import contextvars
import uuid

from spotify_api.context import (
    bind_request_id,
    get_account_id,
    get_request_id,
    new_request_id,
    set_account_id,
)


def test_nothing_is_bound_outside_a_request() -> None:
    fresh = contextvars.Context()
    assert fresh.run(get_request_id) is None
    assert fresh.run(get_account_id) is None


def test_a_request_id_is_bound_for_the_block_and_the_previous_one_restored() -> None:
    def nested() -> list[str | None]:
        seen: list[str | None] = []
        with bind_request_id("outer"):
            with bind_request_id("inner") as bound:
                seen.extend([bound, get_request_id()])
            seen.append(get_request_id())
        seen.append(get_request_id())
        return seen

    assert contextvars.Context().run(nested) == ["inner", "inner", "outer", None]


def test_new_request_ids_are_distinct_uuid4s() -> None:
    first, second = new_request_id(), new_request_id()
    assert first != second
    assert uuid.UUID(first).version == 4


async def test_concurrent_requests_never_see_each_others_account() -> None:
    # Every log record's account_id rests on this.
    both_bound = asyncio.Barrier(2)

    async def serve(account_id: str) -> str | None:
        set_account_id(account_id)
        await both_bound.wait()
        return get_account_id()

    assert list(await asyncio.gather(serve("account-a"), serve("account-b"))) == [
        "account-a",
        "account-b",
    ]


async def test_a_background_job_inherits_the_request_that_started_it() -> None:
    async def job() -> tuple[str | None, str | None]:
        return get_request_id(), get_account_id()

    async def request() -> tuple[str | None, str | None]:
        set_account_id("account-a")
        with bind_request_id("req-1"):
            task = asyncio.create_task(job())
        return await task

    assert await asyncio.create_task(request()) == ("req-1", "account-a")
