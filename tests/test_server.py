"""Tests that need no nemlig.com credentials and make no network calls."""

from __future__ import annotations

import io
import sys
from unittest import mock

import pytest

from nemlig_mcp._compat import quiet
from nemlig_mcp.privacy import REDACTED, scrub
from nemlig_mcp.server import server, set_basket_quantity


@pytest.mark.anyio
async def test_expected_tools_are_registered():
    names = {tool.name for tool in await server.list_tools()}
    assert names == {
        "search_products",
        "get_product_details",
        "get_basket",
        "set_basket_quantity",
        "get_order_history",
        "get_order_details",
    }


@pytest.mark.anyio
async def test_no_tool_can_place_an_order_or_read_cards():
    """Guard the allowlist: checkout and payment must stay off the surface."""
    names = {tool.name for tool in await server.list_tools()}
    forbidden = {"place_order", "checkout", "get_credit_cards", "register_payment"}
    assert not (names & forbidden)


@pytest.mark.anyio
async def test_only_set_basket_quantity_is_a_write():
    writes = {
        tool.name
        for tool in await server.list_tools()
        if not (tool.annotations and tool.annotations.read_only_hint)
    }
    assert writes == {"set_basket_quantity"}


@pytest.mark.anyio
async def test_set_basket_quantity_is_flagged_destructive():
    """The quantity is absolute, so lowering it deletes units. Clients rely on
    this hint to decide whether to confirm with the user first."""
    tool = next(t for t in await server.list_tools() if t.name == "set_basket_quantity")
    assert tool.annotations.destructive_hint is True


@pytest.mark.anyio
async def test_set_basket_quantity_describes_absolute_semantics():
    """A model reading 'how many to add' would silently truncate a line."""
    tool = next(t for t in await server.list_tools() if t.name == "set_basket_quantity")
    assert "absolute" in tool.description.lower()
    assert "0 removes" in tool.description


def test_set_basket_quantity_allows_zero_but_not_negative():
    """0 is the only way to remove a product, so it must not be rejected."""
    with pytest.raises(ValueError):
        set_basket_quantity("701025", -1)

    # 0 must get past validation and reach the API layer; stop it there rather
    # than talking to nemlig.com, so this stays a credential-free unit test.
    calls: list[tuple[str, int]] = []

    class Boom(Exception):
        pass

    def fake_call(fn, *args):
        calls.append(args)
        raise Boom

    with mock.patch("nemlig_mcp.server.get_client") as get_client:
        get_client.return_value.call = fake_call
        with pytest.raises(Boom):
            set_basket_quantity("701025", 0)

    assert calls == [("701025", 0)]


def test_scrub_removes_addresses_but_keeps_line_items():
    payload = {
        "BasketGuid": "abc",
        "DeliveryAddress": {"StreetName": "Vesterbrogade", "MobileNumber": "+4512345678"},
        "InvoiceAddress": {"FirstName": "Anders"},
        "Lines": [{"Id": "701025", "Name": "Cocio kakaomælk", "Price": 23.75}],
    }
    result = scrub(payload)

    assert result["DeliveryAddress"] == REDACTED
    assert result["InvoiceAddress"] == REDACTED
    assert result["Lines"] == payload["Lines"]
    assert result["BasketGuid"] == "abc"


def test_scrub_reaches_into_nested_orders():
    payload = {"Orders": [{"Id": 1, "DeliveryAddress": {"StreetName": "x"}, "Total": 10.0}]}
    result = scrub(payload)

    assert result["Orders"][0]["DeliveryAddress"] == REDACTED
    assert result["Orders"][0]["Total"] == 10.0


def test_quiet_keeps_stdout_clean():
    """The CLI prints spinner frames; stdout is the JSON-RPC transport."""
    captured = io.StringIO()
    real_stdout, sys.stdout = sys.stdout, captured
    try:
        with quiet():
            print("spinner frame that must not reach the client")
    finally:
        sys.stdout = real_stdout

    assert captured.getvalue() == ""
