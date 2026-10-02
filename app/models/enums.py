"""Allowed values for status/period columns, shared by models and schemas."""

from typing import Literal, get_args

BillingPeriod = Literal["one_time", "monthly", "yearly"]
OrderStatus = Literal["pending", "processing", "completed", "cancelled"]
TicketStatus = Literal["open", "in_progress", "resolved", "closed"]

BILLING_PERIODS = get_args(BillingPeriod)
ORDER_STATUSES = get_args(OrderStatus)
TICKET_STATUSES = get_args(TicketStatus)


def sql_in(column: str, values: tuple[str, ...]) -> str:
    """`column IN ('a', 'b')` for CHECK constraints."""
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"
