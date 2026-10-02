"""
app/services/tools.py

The tools the chat model may call, and the code that runs them.

SECURITY MODEL -- read before adding a tool:

* The model chooses *which* tool and *what search/lookup arguments*. It never
  chooses *whose* data. Account tools read the customer from `ToolContext`,
  which the API layer fills from the verified login token. No argument model
  contains a customer id, and unknown arguments are dropped, so a model (or a
  prompt-injected model) that invents `customer_id` has no effect.
* Tools are READ-ONLY. Nothing here changes data.
* Anonymous callers are only offered public tools, and `run_tool` re-checks
  that even if the model names a tool it wasn't offered.
* Arguments are validated; tool failures become an error *result* the model
  can read, never an unhandled exception. Results are capped in size.
* Tool results are DATA. The system prompt tells the model never to follow
  instructions found inside them (a ticket description is user-written text).
"""

import json
import logging
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable

from pydantic import BaseModel, Field, ValidationError

from app.models.enums import OrderStatus, TicketStatus

logger = logging.getLogger(__name__)

MAX_RESULT_CHARS = 6000
MAX_TEXT_CHARS = 200


@dataclass
class ToolContext:
    db: object
    customer: dict | None  # the AUTHENTICATED customer, or None if signed out


# --------------------------------------------------------------------------
# Argument models (these also generate the JSON schema shown to the model)
# --------------------------------------------------------------------------


class SearchProductsArgs(BaseModel):
    query: str | None = Field(
        None,
        description="1-3 keywords, e.g. 'storage' or 'backup drive'. Omit to list everything.",
    )
    max_price: float | None = Field(
        None, ge=0, description="Only products priced at or below this amount (USD)."
    )
    in_stock_only: bool = Field(False, description="Only products that can be bought right now.")
    limit: int = Field(10, ge=1, le=10, description="Maximum products to return.")


class GetOrderArgs(BaseModel):
    order_id: int = Field(..., ge=1, description="The order number to look up.")


class ListOrdersArgs(BaseModel):
    status: OrderStatus | None = Field(None, description="Only orders with this status.")
    limit: int = Field(10, ge=1, le=10, description="Maximum orders to return, newest first.")


class ListTicketsArgs(BaseModel):
    status: TicketStatus | None = Field(None, description="Only tickets with this status.")
    limit: int = Field(10, ge=1, le=10, description="Maximum tickets to return, newest first.")


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

_STOPWORDS = {
    "the", "and", "for", "any", "you", "your", "with", "that", "this", "what",
    "which", "are", "can", "how", "much", "does", "cost", "have", "has", "under",
    "over", "below", "above", "less", "more", "than", "dollar", "dollars", "per",
    "month", "monthly", "year", "yearly", "price", "priced", "cheap", "cheapest",
    "buy", "sell", "sale", "get", "there", "some", "all", "one",
}


def search_terms(query: str | None) -> list[str]:
    """Turn free text from the model into a few clean keywords: lower-case,
    drop filler words, and strip a plural 's' so "plans" matches "plan"."""
    terms: list[str] = []
    for word in re.findall(r"[a-z0-9]+", (query or "").lower()):
        if len(word) < 3 or word in _STOPWORDS:
            continue
        if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        if word not in terms:
            terms.append(word)
    return terms[:5]


def _short(text: str | None) -> str:
    text = text or ""
    return text if len(text) <= MAX_TEXT_CHARS else text[: MAX_TEXT_CHARS - 1].rstrip() + "…"


def _order_out(order: dict) -> dict:
    return {
        "id": order["id"],
        "status": order["status"],
        "total": str(order["total_amount"]),
        "placed": f"{order['created_at']:%Y-%m-%d}",
        "items": [
            {"product": i["product_name"], "quantity": i["quantity"], "unit_price": str(i["unit_price"])}
            for i in order["items"]
        ],
    }


def _error(message: str) -> str:
    return json.dumps({"error": message})


# --------------------------------------------------------------------------
# Handlers. Signature: (ctx, validated_args) -> JSON-serialisable dict
# --------------------------------------------------------------------------


def _search_products(ctx: ToolContext, args: SearchProductsArgs) -> dict:
    max_price = Decimal(str(args.max_price)) if args.max_price is not None else None
    rows, total = ctx.db.search_products(
        terms=search_terms(args.query),
        max_price=max_price,
        in_stock_only=args.in_stock_only,
        limit=args.limit,
    )
    products = []
    for p in rows:
        item = {
            "sku": p["sku"],
            "name": p["name"],
            "description": _short(p["description"]),
            "price": str(p["price"]),
            "billing_period": p["billing_period"],
            "availability": "out of stock" if p["stock_quantity"] == 0 else "in stock",
        }
        if p["stock_quantity"] is not None:
            item["units_left"] = p["stock_quantity"]
        products.append(item)
    return {"total_matches": total, "shown": len(products), "products": products}


def _get_order(ctx: ToolContext, args: GetOrderArgs) -> dict:
    # Scoped to the authenticated customer. "Someone else's order" and "no such
    # order" produce the identical answer, so order ids can't be probed.
    order = ctx.db.get_order_for_customer(args.order_id, ctx.customer["id"])
    if order is None:
        return {
            "found": False,
            "message": f"No order with ID {args.order_id} exists on this customer's account.",
        }
    return {"found": True, "order": _order_out(order)}


def _list_my_orders(ctx: ToolContext, args: ListOrdersArgs) -> dict:
    orders = ctx.db.get_orders_for_customer(ctx.customer["id"], status=args.status, limit=args.limit)
    return {"count": len(orders), "orders": [_order_out(o) for o in orders]}


def _list_my_tickets(ctx: ToolContext, args: ListTicketsArgs) -> dict:
    tickets = ctx.db.get_tickets_for_customer(ctx.customer["id"], status=args.status, limit=args.limit)
    return {
        "count": len(tickets),
        "tickets": [
            {
                "id": t["id"],
                "subject": _short(t["subject"]),
                "status": t["status"],
                "description": _short(t["description"]),
                "opened": f"{t['created_at']:%Y-%m-%d}",
            }
            for t in tickets
        ],
    }


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------


def _clean_schema(node):
    """Trim pydantic's JSON schema for the model: drop titles and null
    defaults, and turn `anyOf: [X, null]` into plain X (omitting an optional
    argument already means "not set"). Simpler schemas are followed more
    reliably by smaller models."""
    if isinstance(node, list):
        return [_clean_schema(x) for x in node]
    if not isinstance(node, dict):
        return node

    any_of = node.get("anyOf")
    if any_of:
        non_null = [x for x in any_of if x.get("type") != "null"]
        if len(non_null) == 1 and len(any_of) == 2:
            rest = {k: v for k, v in node.items() if k != "anyOf"}
            return _clean_schema({**rest, **non_null[0]})

    cleaned = {}
    for key, value in node.items():
        if key == "title" or (key == "default" and value is None):
            continue
        cleaned[key] = _clean_schema(value)
    return cleaned


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args_model: type[BaseModel]
    handler: Callable[[ToolContext, BaseModel], dict]
    requires_login: bool

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": _clean_schema(self.args_model.model_json_schema()),
            },
        }


ALL_TOOLS: dict[str, Tool] = {
    t.name: t
    for t in [
        Tool(
            "search_products",
            "Search the live product catalog for prices and availability. Use this for any "
            "question about what is sold, what something costs, or whether it is in stock.",
            SearchProductsArgs,
            _search_products,
            requires_login=False,
        ),
        Tool(
            "get_order",
            "Look up ONE of the signed-in customer's own orders by its order number: "
            "status, items and total.",
            GetOrderArgs,
            _get_order,
            requires_login=True,
        ),
        Tool(
            "list_my_orders",
            "List the signed-in customer's own orders, newest first, optionally filtered by "
            "status. Use for 'what did I order / buy' questions.",
            ListOrdersArgs,
            _list_my_orders,
            requires_login=True,
        ),
        Tool(
            "list_my_tickets",
            "List the signed-in customer's own support tickets, newest first, optionally "
            "filtered by status.",
            ListTicketsArgs,
            _list_my_tickets,
            requires_login=True,
        ),
    ]
}


def tools_for(customer: dict | None) -> dict[str, Tool]:
    """The tools this caller may be offered. Signed-out callers get only
    public tools -- account tools aren't even described to the model."""
    return {
        name: tool
        for name, tool in ALL_TOOLS.items()
        if customer is not None or not tool.requires_login
    }


def _parse_arguments(raw) -> dict:
    """Providers return arguments as a JSON string, sometimes empty or
    already a dict. Anything that isn't a JSON object is rejected."""
    if raw is None or raw == "":
        return {}
    if isinstance(raw, dict):
        return raw
    parsed = json.loads(raw)
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise ValueError("arguments must be a JSON object")
    return parsed


def run_tool(tools: dict[str, Tool], name: str, raw_arguments, ctx: ToolContext) -> str:
    """Execute one tool call and return the result as a JSON string for the
    model. Never raises."""
    tool = tools.get(name)
    if tool is None:
        return _error(f"Unknown tool '{name}'. Available tools: {sorted(tools)}")
    if tool.requires_login and ctx.customer is None:
        return _error("This tool needs a signed-in customer.")

    try:
        args = tool.args_model.model_validate(_parse_arguments(raw_arguments))
    except ValidationError as error:
        first = error.errors()[0]
        field = ".".join(str(p) for p in first["loc"]) or "arguments"
        return _error(f"Invalid argument '{field}': {first['msg']}")
    except ValueError as error:  # bad JSON / not an object
        return _error(f"Invalid arguments: {error}")

    try:
        result = tool.handler(ctx, args)
    except Exception:
        logger.exception("Tool %s failed", name)
        return _error("The lookup failed. Tell the customer you can't check that right now.")

    text = json.dumps(result, default=str)
    if len(text) > MAX_RESULT_CHARS:
        return _error("The result was too large. Ask for fewer or narrower results.")
    return text
