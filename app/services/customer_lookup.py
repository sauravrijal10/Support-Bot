"""
app/services/customer_lookup.py

Detects questions that need someone's account data. Used ONLY for the
signed-out gate in chat_service: a signed-out caller who asks about "my
order", an order number, or an email gets a "please log in" reply instead
of a model call.

Fetching the signed-in customer's data is done by tools (app/services/
tools.py), scoped to the authenticated customer -- not by anything in
this file.
"""

import re

# Quantifiers are bounded and adjacent classes don't overlap, so matching
# stays linear-time even on adversarial input (this runs on anonymous
# requests).
EMAIL_RE = re.compile(r"[\w.+-]{1,64}@[\w-]{1,63}(?:\.[\w-]{1,63}){1,8}")
ORDER_ID_RE = re.compile(r"order\s{0,5}#?\s{0,5}(\d{1,9})|#(\d{1,9})", re.IGNORECASE)
PERSONAL_RE = re.compile(
    r"\bmy (order|account|subscription|plan|ticket|billing|invoice)\b",
    re.IGNORECASE,
)


def extract_email(query):
    match = EMAIL_RE.search(query)
    return match.group(0) if match else None


def extract_order_id(query):
    match = ORDER_ID_RE.search(query)
    if not match:
        return None
    return int(match.group(1) or match.group(2))


def looks_personal(query):
    """True if the question seems to be asking about the customer's own data."""
    return bool(PERSONAL_RE.search(query))


def needs_account_access(query):
    """True if answering would require someone's account data."""
    return (
        looks_personal(query)
        or extract_order_id(query) is not None
        or extract_email(query) is not None
    )
