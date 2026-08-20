"""Tests that need no nemlig.com credentials and make no network calls."""

from __future__ import annotations

import contextlib
import io
import sys
from unittest import mock

import nemlig_cli
import pytest
import requests
from pydantic import ValidationError

from nemlig_mcp import server as server_module
from nemlig_mcp._compat import quiet
from nemlig_mcp.privacy import REDACTED, scrub
from nemlig_mcp.server import (
    MAX_BATCH_ITEMS,
    MAX_RETRIES,
    BasketItem,
    server,
    set_basket_quantities,
    set_basket_quantity,
)


@pytest.fixture(autouse=True)
def no_retry_backoff(monkeypatch):
    """Keep the retry tests instant; the wait itself is not what they assert."""
    monkeypatch.setattr(server_module, "RETRY_BACKOFF_SECONDS", 0)


@pytest.mark.anyio
async def test_expected_tools_are_registered():
    names = {tool.name for tool in await server.list_tools()}
    assert names == {
        "search_products",
        "get_product_details",
        "get_basket",
        "set_basket_quantity",
        "set_basket_quantities",
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
async def test_only_the_basket_setters_are_writes():
    writes = {
        tool.name
        for tool in await server.list_tools()
        if not (tool.annotations and tool.annotations.read_only_hint)
    }
    assert writes == {"set_basket_quantity", "set_basket_quantities"}


@pytest.mark.anyio
@pytest.mark.parametrize("name", ["set_basket_quantity", "set_basket_quantities"])
async def test_basket_setters_are_flagged_destructive(name):
    """The quantity is absolute, so lowering it deletes units. Clients rely on
    this hint to decide whether to confirm with the user first."""
    tool = next(t for t in await server.list_tools() if t.name == name)
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


@contextlib.contextmanager
def fake_client(add_to_basket, get_basket=lambda: {"Lines": []}):
    """Run the batch tool against stubs instead of nemlig.com.

    ``call`` dispatches on the CLI function the server passed it, mirroring
    ``NemligClient.call(func, *args)`` minus the auth argument.
    """

    def call(fn, *args):
        if fn is nemlig_cli.get_basket:
            return get_basket()
        return add_to_basket(*args)

    with mock.patch("nemlig_mcp.server.get_client") as get_client:
        get_client.return_value.call = call
        yield


def http_error(status: int) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError(f"{status} error", response=response)


def test_set_basket_quantities_applies_items_in_order():
    calls: list[tuple[str, int]] = []

    def add(product_id, quantity):
        calls.append((product_id, quantity))
        return {"Lines": []}

    with fake_client(add, get_basket=lambda: {"Lines": [{"Id": "701025"}]}):
        result = set_basket_quantities(
            [BasketItem(product_id="701025", quantity=2), BasketItem(product_id="5070417")]
        )

    assert calls == [("701025", 2), ("5070417", 1)]
    assert [r["status"] for r in result["results"]] == ["ok", "ok"]
    assert [r["attempts"] for r in result["results"]] == [1, 1]
    assert result["basket"] == {"Lines": [{"Id": "701025"}]}
    assert result["basket_error"] is None


def test_set_basket_quantities_retries_a_transient_failure_then_succeeds():
    attempts: list[str] = []

    def add(product_id, quantity):
        attempts.append(product_id)
        if len(attempts) <= MAX_RETRIES:
            raise http_error(503)
        return {"Lines": []}

    with fake_client(add):
        result = set_basket_quantities([BasketItem(product_id="701025", quantity=1)])

    assert len(attempts) == MAX_RETRIES + 1, "the last retry in the budget must still run"
    assert result["results"][0]["status"] == "ok"
    assert result["results"][0]["attempts"] == MAX_RETRIES + 1
    assert result["results"][0]["error"] is None


def test_set_basket_quantities_gives_up_after_three_retries():
    attempts: list[str] = []

    def add(product_id, quantity):
        attempts.append(product_id)
        raise http_error(503)

    with fake_client(add):
        result = set_basket_quantities([BasketItem(product_id="701025", quantity=1)])

    assert len(attempts) == 4, "one initial request plus three retries"
    assert result["results"][0]["status"] == "failed"
    assert result["results"][0]["attempts"] == 4
    assert "503" in result["results"][0]["error"]


def test_set_basket_quantities_does_not_retry_a_rejected_request():
    """A 400 means nemlig refused the request; asking twice more only stalls
    the rest of the batch."""
    attempts: list[str] = []

    def add(product_id, quantity):
        attempts.append(product_id)
        raise http_error(400)

    with fake_client(add):
        result = set_basket_quantities([BasketItem(product_id="nope", quantity=1)])

    assert len(attempts) == 1
    assert result["results"][0] == {
        "product_id": "nope",
        "quantity": 1,
        "status": "failed",
        "attempts": 1,
        "error": mock.ANY,
    }


def test_set_basket_quantities_continues_past_a_failed_item():
    """One bad product must not discard what the rest of the batch did."""
    def add(product_id, quantity):
        if product_id == "bad":
            raise http_error(400)
        return {"Lines": []}

    with fake_client(add, get_basket=lambda: {"Lines": [{"Id": "good"}]}):
        result = set_basket_quantities(
            [
                BasketItem(product_id="bad", quantity=1),
                BasketItem(product_id="good", quantity=2),
            ]
        )

    assert [r["status"] for r in result["results"]] == ["failed", "ok"]
    assert result["basket"] == {"Lines": [{"Id": "good"}]}


def test_set_basket_quantities_reports_an_unreadable_basket_without_losing_results():
    """The basket has already been changed by then; dropping the per-item
    report would leave the model blind to what it just did."""
    def unreadable():
        raise http_error(500)

    with fake_client(lambda *args: {"Lines": []}, get_basket=unreadable):
        result = set_basket_quantities([BasketItem(product_id="701025", quantity=1)])

    assert result["results"][0]["status"] == "ok"
    assert result["basket"] is None
    assert "500" in result["basket_error"]


def test_set_basket_quantities_scrubs_the_returned_basket():
    with fake_client(
        lambda *args: {"Lines": []},
        get_basket=lambda: {"DeliveryAddress": {"StreetName": "x"}, "Lines": []},
    ):
        result = set_basket_quantities([BasketItem(product_id="701025", quantity=1)])

    assert result["basket"]["DeliveryAddress"] == REDACTED


def test_set_basket_quantities_rejects_a_repeated_product():
    """Quantities are absolute, so two lines for one product would silently
    mean 'last one wins' rather than the sum a model likely intended."""
    with pytest.raises(ValueError, match="only once"):
        set_basket_quantities(
            [
                BasketItem(product_id="701025", quantity=2),
                BasketItem(product_id="701025", quantity=3),
            ]
        )


def test_set_basket_quantities_rejects_empty_and_oversized_batches():
    with pytest.raises(ValueError, match="must not be empty"):
        set_basket_quantities([])

    too_many = [BasketItem(product_id=str(i)) for i in range(MAX_BATCH_ITEMS + 1)]
    with pytest.raises(ValueError, match="at most 50"):
        set_basket_quantities(too_many)


def test_basket_item_rejects_a_negative_quantity():
    with pytest.raises(ValidationError):
        BasketItem(product_id="701025", quantity=-1)


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
