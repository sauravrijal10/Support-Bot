from typing import Annotated, Literal

from pydantic import BaseModel, Field

Question = Annotated[
    str, Field(min_length=1, max_length=2000, description="The customer's question")
]


class ChatTurn(BaseModel):
    """One earlier exchange in the conversation."""

    question: str = Field(..., max_length=2000)
    answer: str = Field(..., max_length=4000)


class ChatRequest(BaseModel):
    question: Question
    history: list[ChatTurn] = Field(
        default_factory=list,
        max_length=20,
        description=(
            "Earlier turns of this conversation, oldest first, so follow-up "
            "questions have context. Optional; the most recent few are used."
        ),
    )


class ChatResponse(BaseModel):
    answer: str
    sources: list[str] = []
    needs_authentication: bool = Field(
        default=False,
        description=(
            "True when the question needs account data but no valid "
            "bearer token was sent -- log in via POST /auth/login and retry."
        ),
    )
    tools_used: list[str] = Field(
        default_factory=list,
        description="Names of the lookup tools the assistant called to answer (for transparency).",
    )


# ------------------------------------------------ WebSocket frames (client -> server)


class WSAuth(BaseModel):
    """Sign in (or, with token=None, sign out) on an open connection.
    Changing identity starts a fresh conversation."""

    type: Literal["auth"]
    token: str | None = None


class WSMessage(BaseModel):
    type: Literal["message"]
    question: Question


class WSReset(BaseModel):
    """Forget the conversation so far."""

    type: Literal["reset"]


class WSPing(BaseModel):
    type: Literal["ping"]


WSClientFrame = Annotated[WSAuth | WSMessage | WSReset | WSPing, Field(discriminator="type")]
