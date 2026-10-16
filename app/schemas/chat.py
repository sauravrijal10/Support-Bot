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


class OrderFormOption(BaseModel):
    sku: str
    name: str
    unit_price: str
    billing_period: str
    max_quantity: int = Field(
        description="Most that can be ordered (stock or the per-order cap); 0 = sold out"
    )


class OrderFormLine(BaseModel):
    sku: str = Field(description="The option currently selected")
    quantity: int
    options: list[OrderFormOption] = Field(description="The product's variants, cheapest first")


class OrderForm(BaseModel):
    """An order the assistant prepared. Show it as a form (option, quantity,
    live total), then POST /orders/drafts/{draft_id}/confirm or /discard."""

    kind: Literal["order", "cart"] = Field(
        default="order",
        description="'order': what the customer asked to buy. 'cart': products they were "
        "shown, every quantity 0 -- send only the lines they gave a quantity.",
    )
    draft_id: int
    lines: list[OrderFormLine]
    total: str
    expires_at: str


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
    order_form: OrderForm | None = Field(
        default=None,
        description="Present when the assistant prepared an order to confirm, or showed "
        "products a signed-in customer can order.",
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
