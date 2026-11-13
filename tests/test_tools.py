"""End-to-end tests of every tool, using the fake price source."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from pokemon_mcp.db import CollectionError
from pokemon_mcp.models import SetInfo, ValidationError
from pokemon_mcp.server import build_server
from pokemon_mcp.sources.base import CardNotFoundError
from tests.conftest import make_card


# --- search_card -------------------------------------------------------------

async def test_search_card(service, source):
    source.add(make_card("base1-4"))
    source.add(make_card("base1-2", name="Blastoise"))
    out = await service.search_card("chari")
    assert [r["card_id"] for r in out["results"]] == ["base1-4"]
    assert out["results"][0]["market_price_usd"] == 100.0


async def test_search_rejects_query_injection(service, source):
    with pytest.raises(ValidationError):
        await service.search_card('x" OR name:"*')
    assert source.calls == []  # never reached the API


# --- add / remove ------------------------------------------------------------

async def test_add_card_records_lot_and_snapshot(service, source, db, clock):
    source.add(make_card("base1-4", price=300.0))
    out = await service.add_card("base1-4", "lp", 2, 250)
    assert out["total_owned"] == 2 and out["condition"] == "LP"
    assert db.latest_snapshot("base1-4").price == 300.0


async def test_add_unknown_card_fails_and_writes_nothing(service, db):
    with pytest.raises(CardNotFoundError):
        await service.add_card("nope-1")
    assert db.holdings() == []


async def test_remove_card(service, source):
    source.add(make_card("base1-4"))
    await service.add_card("base1-4", "NM", 3)
    out = await service.remove_card("base1-4", 2)
    assert out["remaining"] == 1
    with pytest.raises(CollectionError):
        await service.remove_card("base1-4", 5)


# --- collection_value --------------------------------------------------------

async def test_collection_value(service, source):
    source.add(make_card("base1-4", price=100.0))
    source.add(make_card("base1-2", name="Blastoise", price=40.0, number="2"))
    await service.add_card("base1-4", "NM", 2, 80.0)   # cost 160, value 200
    await service.add_card("base1-2", "NM", 1)         # no cost, value 40
    out = await service.collection_value()
    assert out["total_value"] == 240.0
    assert out["cost_basis"] == 160.0
    assert out["gain_loss"] == 40.0         # only copies with a known cost
    assert out["gain_loss_pct"] == 25.0
    assert any("no purchase price" in n for n in out["notes"])


async def test_collection_value_empty(service):
    out = await service.collection_value()
    assert out["total_value"] == 0 and out["gain_loss_pct"] is None


# --- snapshot_prices / price_history / top_movers ----------------------------

async def test_snapshot_prices_keeps_going_on_failure(service, source, db):
    source.add(make_card("base1-4", price=100.0))
    source.add(make_card("base1-2", name="Blastoise", price=None, number="2"))
    await service.add_card("base1-4")
    await service.add_card("base1-2")
    source.add(make_card("base1-15", name="Venusaur", number="15"))
    await service.add_card("base1-15")
    del source.cards["base1-15"]  # API now fails for this card

    out = await service.snapshot_prices()
    assert out["saved"] == 1
    assert out["no_price"] == ["base1-2"]
    assert out["failed"][0]["card_id"] == "base1-15"


async def test_price_history_and_top_movers(service, source, db, clock):
    source.add(make_card("base1-4", price=100.0))
    source.add(make_card("base1-2", name="Blastoise", price=50.0, number="2"))
    await service.add_card("base1-4", quantity=2)
    await service.add_card("base1-2")  # day 0 snapshots: 100 and 50

    clock.value += timedelta(days=7)
    source.add(make_card("base1-4", price=150.0))
    source.add(make_card("base1-2", name="Blastoise", price=40.0, number="2"))
    await service.snapshot_prices()

    hist = await service.price_history("base1-4", 30)
    assert [p["price"] for p in hist["points"]] == [100.0, 150.0]
    assert hist["change_pct"] == 50.0

    movers = await service.top_movers(7)
    assert movers["gainers"][0]["card_id"] == "base1-4"
    assert movers["gainers"][0]["position_change"] == 100.0
    assert movers["losers"][0]["card_id"] == "base1-2"
    assert movers["losers"][0]["change_pct"] == -20.0


async def test_price_history_empty_has_hint(service):
    out = await service.price_history("base1-4", 7)
    assert out["points"] == [] and "snapshot_prices" in out["note"]


async def test_top_movers_needs_two_days(service, source):
    source.add(make_card("base1-4"))
    await service.add_card("base1-4")
    out = await service.top_movers(7)
    assert out["gainers"] == [] and "note" in out


# --- set_progress ------------------------------------------------------------

async def test_set_progress(service, source):
    cards = [make_card(f"base1-{n}", name=f"Card {n}", number=str(n)) for n in (1, 2, 10, 3)]
    source.sets["base"] = SetInfo(id="base1", name="Base", cards=cards)
    source.add(cards[0])
    await service.add_card("base1-1")
    out = await service.set_progress("Base")
    assert (out["owned"], out["total"], out["percent_complete"]) == (1, 4, 25.0)
    assert [m["number"] for m in out["missing"]] == ["2", "3", "10"]  # natural order


# --- MCP layer ---------------------------------------------------------------

async def test_tools_are_labelled_read_only_or_write(service):
    tools = {t.name: t for t in await build_server(service).list_tools()}
    read_only = {"search_card", "collection_value", "price_history", "top_movers", "set_progress"}
    write = {"add_card", "remove_card", "snapshot_prices"}
    assert set(tools) == read_only | write
    for name in read_only:
        assert tools[name].annotations.readOnlyHint is True
        assert tools[name].description.startswith("[READ-ONLY]")
    for name in write:
        assert tools[name].annotations.readOnlyHint is False
        assert tools[name].description.startswith("[WRITE]")
    assert tools["remove_card"].annotations.destructiveHint is True


async def test_tool_call_end_to_end(service, source):
    source.add(make_card("base1-4"))
    mcp = build_server(service)
    await mcp.call_tool("add_card", {"card_id": "base1-4", "quantity": 2, "purchase_price": 50})
    result = await mcp.call_tool("collection_value", {})
    assert "200.0" in str(result)


async def test_tool_errors_are_clean(service):
    mcp = build_server(service)
    with pytest.raises(ToolError, match="card_id must look like"):
        await mcp.call_tool("add_card", {"card_id": "x'; DROP TABLE cards;--"})


async def test_unexpected_errors_are_hidden(service, monkeypatch):
    async def boom():
        raise RuntimeError("secret internal detail /home/user/db")

    monkeypatch.setattr(service, "collection_value", boom)
    mcp = build_server(service)
    with pytest.raises(ToolError) as exc:
        await mcp.call_tool("collection_value", {})
    assert "secret" not in str(exc.value)
