"""MCP server exposing nemlig.com grocery shopping.

The tool surface is a deliberate allowlist over what nemlig_cli implements.
Notably absent: order placement and payment-card access. The CLI does not
implement either, and this server will not add them -- an MCP tool that can
charge a saved credit card is not something a model should hold.
"""

from __future__ import annotations

from typing import Any

import nemlig_cli
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from .client import CredentialsError, NemligClient
from .privacy import scrub

server = MCPServer(
    name="nemlig",
    version="0.1.0",
    instructions=(
        "Search and shop groceries on nemlig.com (Danish online supermarket). "
        "Prices are in DKK. Product names and categories are in Danish. "
        "This server can read the basket and add items to it, but cannot place "
        "an order -- the user must complete checkout themselves on nemlig.com."
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
WRITES = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)


@server.tool(annotations=READ_ONLY)
def search_products(query: str, limit: int = 10) -> list[dict[str, Any]]:
    """Search the nemlig.com catalogue.

    Args:
        query: Search term. Danish terms match best, e.g. "kaffebønner", "mælk".
        limit: Maximum number of products to return (1-50).

    Returns:
        Matching products with Id, Name, Brand, Price and availability. Use the
        Id with get_product_details or add_to_basket.
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
def add_to_basket(product_id: str, quantity: int = 1) -> dict[str, Any]:
    """Add a product to the shopping basket.

    This changes the user's real basket on nemlig.com. It does not place an
    order or charge anything -- the user completes checkout themselves.

    Args:
        product_id: Product Id from search_products, e.g. "5070417".
        quantity: How many units to add. Must be positive.

    Returns:
        The updated basket, with addresses redacted.
    """
    if quantity < 1:
        raise ValueError(f"quantity must be at least 1, got {quantity}")
    return scrub(get_client().call(nemlig_cli.add_to_basket, product_id, quantity))


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
