"""The batch lookup endpoint."""

from __future__ import annotations

from fastapi import APIRouter, status
from fastapi.responses import JSONResponse

from spotify_api.api.asynchrony import AsyncFlag, run_or_submit
from spotify_api.api.dependencies import (
    JobRunnerDep,
    PreferencesDep,
    ResolverDep,
    UserContextDep,
)
from spotify_api.context import get_request_id, new_request_id
from spotify_api.errors import BatchTooLargeError
from spotify_api.models.requests import LookupRequest
from spotify_api.models.responses import LookupResponse, Problem

__all__ = ["router"]

router = APIRouter(tags=["lookup"])


@router.post(
    "/lookup",
    response_model=LookupResponse,
    summary="Resolve a batch of tracks",
    description=(
        "Takes an array of loosely-specified tracks and resolves each one "
        "against the Spotify Web API.\n\n"
        "Only `name` is required per item; `artist`, `album` and `year` are "
        "optional hints that narrow the search and materially improve accuracy "
        "for common titles.\n\n"
        "Responds with **partial success**: exactly one result per submitted "
        "item, in the order submitted, each carrying its own `found`, "
        "`not_found` or `error` status. A single unresolvable track never "
        "fails the request."
    ),
    responses={
        status.HTTP_202_ACCEPTED: {
            "description": "Accepted for background execution (`?async=true`).",
        },
        status.HTTP_401_UNAUTHORIZED: {
            "model": Problem,
            "description": "The keyring user token is missing or was refused.",
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": Problem,
            "description": "The body is invalid, or the batch is over this person's cap.",
        },
        status.HTTP_502_BAD_GATEWAY: {
            "model": Problem,
            "description": "keyring has no usable Spotify credential for this profile.",
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": Problem,
            "description": "Spotify or keyring is unreachable.",
        },
    },
)
async def lookup(
    payload: LookupRequest,
    resolver: ResolverDep,
    preferences: PreferencesDep,
    context: UserContextDep,
    runner: JobRunnerDep,
    run_async: AsyncFlag = False,
) -> LookupResponse | JSONResponse:
    """Resolve every submitted item and return one result per item.

    The schema caps batches at an absolute ceiling; this is the *operational*
    cap, which is per person (inside this deployment's ceiling) and so cannot
    live in the model.
    """
    if len(payload.items) > preferences.max_batch_size:
        message = (
            f"a batch may contain at most {preferences.max_batch_size} items, "
            f"and this one has {len(payload.items)}"
        )
        raise BatchTooLargeError(
            message, limit=preferences.max_batch_size, received=len(payload.items)
        )

    # Always bound: the context middleware wraps every route. The fallback keeps the type honest.
    request_id = get_request_id() or new_request_id()

    async def work() -> LookupResponse:
        results = await resolver.resolve(
            payload.items,
            market=payload.market,
            default_market=preferences.default_market,
            context=context,
        )
        return LookupResponse(request_id=request_id, results=results)

    return await run_or_submit(
        run_async=run_async,
        operation="lookup",
        account_id=context.account_id,
        work=work,
        runner=runner,
    )
