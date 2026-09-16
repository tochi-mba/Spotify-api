"""What keyring hands back."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, SecretStr

__all__ = ["ResolvedCredential", "UserContext"]


class ResolvedCredential(BaseModel):
    """What to attach to an outgoing Spotify request.

    Deliberately *not* the credential itself: keyring returns ready-made
    headers, so the underlying access token never becomes a thing this service
    has to hold, log or accidentally serialise.
    """

    model_config = ConfigDict(frozen=True)

    headers: dict[str, str] = Field(description="Attach these to the outgoing request.")
    query_params: dict[str, str] = Field(
        default_factory=dict, description="Add these to the query string."
    )


class UserContext(BaseModel):
    """Whose Spotify account a request acts on.

    Threaded through every call rather than held on the client, because one
    process serves many users and a client bound to one of them would be a
    cross-user data leak waiting to happen.
    """

    model_config = ConfigDict(frozen=True)

    account_id: str = Field(
        description=(
            "The verified keyring account id: the token's ``sub``, checked against keyring's "
            "published keys. The only identity this service learns, and the key every piece of "
            "stored state is scoped by."
        )
    )
    user_token: SecretStr = Field(description="The caller's short-lived keyring token.")
    profile: str = Field(description="Which keyring profile to read the credential from.")
