"""The batch lookup endpoint."""

from __future__ import annotations

from fastapi import APIRouter, status

from spotify_api.api.dependencies import ResolverDep, SettingsDep, UserContextDep
from spotify_api.errors import BatchTooLargeError
from spotify_api.logging import current_request_id
from spotify_api.models.requests import LookupRequest
from spotify_api.models.responses import ErrorResponse, LookupResponse

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
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorResponse,
            "description": "The request body is invalid.",
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorResponse,
            "description": "Spotify is unreachable or rejected us.",
        },
    },
)
async def lookup(
    payload: LookupRequest,
    resolver: ResolverDep,
    settings: SettingsDep,
    context: UserContextDep,
) -> LookupResponse:
    """Resolve every submitted item and return one result per item.

    The schema caps batches at an absolute ceiling; this is the *operational*
    cap, which is configurable per deployment and so cannot live in the model.
    """
    if len(payload.items) > settings.max_batch_size:
        message = (
            f"a batch may contain at most {settings.max_batch_size} items, "
            f"and this one has {len(payload.items)}"
        )
        raise BatchTooLargeError(
            message, limit=settings.max_batch_size, received=len(payload.items)
        )

    results = await resolver.resolve(payload.items, market=payload.market, context=context)
    return LookupResponse(request_id=current_request_id(), results=results)
