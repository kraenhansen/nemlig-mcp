"""MCP server exposing nemlig.com grocery shopping.

The tool surface is a deliberate allowlist over what nemlig_cli implements.
Notably absent: order placement and payment-card access. The CLI does not
implement either, and this server will not add them -- an MCP tool that can
charge a saved credit card is not something a model should hold.
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Callable
from typing import Any, TypeVar

import nemlig_cli
import requests
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from .client import CredentialsError, NemligClient
from .privacy import scrub

T = TypeVar("T")

server = MCPServer(
    name="nemlig",
    version="0.1.0",
    instructions=(
        "Search and shop groceries on nemlig.com (Danish online supermarket). "
        "Prices are in DKK. Product names and categories are in Danish. "
        "This server can read the basket and change what is in it, including "
        "removing items, but cannot place an order -- the user must complete "
        "checkout themselves on nemlig.com."
    ),
)

_client: NemligClient | None = None


def get_client() -> NemligClient:
    """Return the process-wide client, constructing it on first use.

    Constructed lazily so the server still starts (and can report the problem
    through a tool error) when credentials are missing.
    """
    global _client
    if _client is None:
        _client = NemligClient()
    return _client


READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=True)
# destructive_hint is True because the quantity is absolute: lowering it drops
# units already in the basket, and 0 removes the line outright. This is not an
# additive-only tool, and a client deciding whether to confirm must know that.
WRITES = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=True)

# Bounds for set_basket_quantities. The batch is applied sequentially, so a
# large list with a slow failure mode could outlive the client's tool timeout.
MAX_BATCH_ITEMS = 50
MAX_RETRIES = 3
MAX_ATTEMPTS = MAX_RETRIES + 1
RETRY_BACKOFF_SECONDS = 0.5


class BasketItem(BaseModel):
    """One product line in a set_basket_quantities call."""

    product_id: str = Field(description='Product Id from search_products, e.g. "5070417".')
    quantity: int = Field(
        default=1,
        ge=0,
        description="Units the basket should end up with. 0 removes the product.",
    )


def _is_retryable(exc: Exception) -> bool:
    """Whether another attempt could plausibly succeed.

    A timeout, a rate limit or a 5xx may pass on a second try. Anything else
    the server rejected outright will be rejected again, so retrying it only
    delays the rest of the batch. 401/403 are excluded here because
    ``NemligClient.call`` already handles those by re-logging in.
    """
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        status = exc.response.status_code
        return status in (408, 429) or status >= 500
    return isinstance(exc, requests.RequestException)


def _retrying(call: Callable[[], T]) -> tuple[T | None, int, str | None]:
    """Run ``call`` until it succeeds or the attempt budget runs out.

    Returns the value, how many attempts were made, and the final error message
    (None on success). Failures are reported rather than raised so that one bad
    product does not discard what the rest of the batch already did.
    """
    error: str | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return call(), attempt, None
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            if not _is_retryable(exc):
                return None, attempt, error
            if attempt < MAX_ATTEMPTS:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
    return None, MAX_ATTEMPTS, error


def _apply_quantity(item: BasketItem) -> dict[str, Any]:
    """Set one product's quantity, reporting the outcome instead of raising."""
    _, attempts, error = _retrying(
        lambda: get_client().call(nemlig_cli.add_to_basket, item.product_id, item.quantity)
    )
    return {
        "product_id": item.product_id,
        "quantity": item.quantity,
        "status": "failed" if error else "ok",
        "attempts": attempts,
        "error": error,
    }


@server.tool(annotations=READ_ONLY)
def search_products(query: str, limit: int = 10) -> list[dict[str, Any]]:
    """Search the nemlig.com catalogue.

    Args:
        query: Search term. Danish terms match best, e.g. "kaffebønner", "mælk".
        limit: Maximum number of products to return (1-50).

    Returns:
        Matching products with Id, Name, Brand, Price and availability. Use the
        Id with get_product_details or set_basket_quantity.
    """
    limit = max(1, min(limit, 50))
    return get_client().call(nemlig_cli.search_products, query, limit)


@server.tool(annotations=READ_ONLY)
def get_product_details(product_id: str) -> dict[str, Any]:
    """Fetch full details for one product, including nutrition and allergens.

    Args:
        product_id: Product Id from search_products, e.g. "5070417".
    """
    try:
        return get_client().call(nemlig_cli.get_product_details, product_id)
    except nemlig_cli.ProductNotFoundError as exc:
        raise ValueError(f"No product with id {product_id!r}: {exc}") from exc


@server.tool(annotations=READ_ONLY)
def get_basket() -> dict[str, Any]:
    """Show the current shopping basket with line items and prices.

    Delivery and invoice addresses are redacted.
    """
    return scrub(get_client().call(nemlig_cli.get_basket))


@server.tool(annotations=WRITES)
def set_basket_quantity(product_id: str, quantity: int = 1) -> dict[str, Any]:
    """Set how many units of a product the basket should contain.

    The quantity is absolute, not a delta. It is the number of units to end up
    with, so setting 2 on a line that currently holds 5 removes 3 of them, and
    setting 0 removes the product entirely. To add to a line that may already
    exist, read its current Quantity with get_basket and pass the new total.

    This changes the user's real basket on nemlig.com. It does not place an
    order or charge anything -- the user completes checkout themselves.

    Args:
        product_id: Product Id from search_products, e.g. "5070417".
        quantity: Units the basket should end up with. 0 removes the product.

    Returns:
        The updated basket, with addresses redacted.
    """
    if quantity < 0:
        raise ValueError(f"quantity must be 0 or greater, got {quantity}")
    return scrub(get_client().call(nemlig_cli.add_to_basket, product_id, quantity))


@server.tool(annotations=WRITES)
def set_basket_quantities(items: list[BasketItem]) -> dict[str, Any]:
    """Set the quantities of several products in one call.

    Each item is applied in order with the same absolute semantics as
    set_basket_quantity: the quantity is the number of units to end up with,
    not a number to add, and 0 removes the product. A product may therefore
    appear only once in the list.

    An item whose request fails is retried up to three times, then recorded
    and skipped. The batch always runs to the end, so a single bad product does
    not hide what happened to the others -- check every entry in "results"
    before reporting success.

    This changes the user's real basket on nemlig.com. It does not place an
    order or charge anything -- the user completes checkout themselves.

    Args:
        items: Products and the quantity each should end up at, applied in the
            given order. At most 50 per call.

    Returns:
        results: one entry per requested item, in order, each with status "ok"
        or "failed", the number of attempts made, and the error if it failed.
        basket: the resulting basket with addresses redacted, or null if it
        could not be read, in which case basket_error says why.
    """
    if not items:
        raise ValueError("items must not be empty")
    if len(items) > MAX_BATCH_ITEMS:
        raise ValueError(f"at most {MAX_BATCH_ITEMS} items per call, got {len(items)}")

    repeated = sorted(pid for pid, n in Counter(i.product_id for i in items).items() if n > 1)
    if repeated:
        raise ValueError(
            "quantities are absolute, not added up, so each product may appear only "
            f"once; pass one line per product. Repeated: {', '.join(repeated)}"
        )

    results = [_apply_quantity(item) for item in items]
    basket, _, basket_error = _retrying(lambda: get_client().call(nemlig_cli.get_basket))

    return {
        "results": results,
        "basket": None if basket is None else scrub(basket),
        "basket_error": basket_error,
    }


@server.tool(annotations=READ_ONLY)
def get_order_history(skip: int = 0, take: int = 10) -> dict[str, Any]:
    """List previous orders, most recent first.

    Args:
        skip: Number of orders to skip, for pagination.
        take: Number of orders to return (1-50).

    Returns:
        Orders with Id, OrderNumber, Total and delivery window. Use Id with
        get_order_details. Addresses are redacted.
    """
    take = max(1, min(take, 50))
    return scrub(get_client().call(nemlig_cli.get_order_history, max(0, skip), take))


@server.tool(annotations=READ_ONLY)
def get_order_details(order_id: int) -> dict[str, Any]:
    """Fetch the line items of one past order.

    Args:
        order_id: Numeric order Id from get_order_history (not OrderNumber).
    """
    return scrub(get_client().call(nemlig_cli.get_order_details, order_id))


def main() -> None:
    """Entry point for the ``nemlig-mcp`` console script."""
    server.run("stdio")


if __name__ == "__main__":
    main()
