"""Strip personal data from API responses before they reach the model.

Basket and order-history responses embed the account holder's full name,
street address, and phone number. None of that is needed to reason about
groceries, and everything an MCP tool returns lands in the model's context and
the conversation transcript. So it is removed by default.
"""

from __future__ import annotations

from typing import Any

# Whole sub-objects that are nothing but personal data.
PERSONAL_BLOCKS = frozenset({"InvoiceAddress", "DeliveryAddress"})

# Individual identifying fields that appear outside those blocks.
PERSONAL_FIELDS = frozenset({"CustomerName", "MobileNumber", "PhoneNumber", "ContactPerson"})

REDACTED = "[redacted by nemlig-mcp]"


def scrub(value: Any) -> Any:
    """Recursively drop personal data from a decoded JSON structure.

    Address blocks are replaced with a marker rather than deleted, so a model
    can still tell that an order *had* a delivery address.
    """
    if isinstance(value, dict):
        return {
            key: REDACTED if key in PERSONAL_BLOCKS or key in PERSONAL_FIELDS else scrub(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [scrub(item) for item in value]
    return value
