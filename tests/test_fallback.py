"""TCGdex price fallback: id translation, USD-only parsing, failover rules,
circuit breaker and price provenance. All HTTP is mocked."""

from __future__ import annotations

import httpx
import pytest

from pokemon_mcp.models import Card
from pokemon_mcp.service import CollectionService
from pokemon_mcp.sources.base import CardNotFoundError, PriceSource, SourceError, SourceUnavailableError
from pokemon_mcp.sources.fallback import FallbackPriceSource
from pokemon_mcp.sources.tcgdex import TCGdexSource, candidate_ids
from tests.conftest import FakePriceSource, make_card

# Real response shape captured from api.tcgdex.net/v2/en/cards/sv03.5-199.
TCGDEX_CHARIZARD = {
    "id": "sv03.5-199", "localId": "199", "name": "Charizard ex",
    "rarity": "Special illustration rare",
    "image": "https://assets.tcgdex.net/en/sv/sv03.5/199",
    "set": {"id": "sv03.5", "name": "151"},
    "pricing": {
        "cardmarket": {"unit": "EUR", "avg": 250.0},
        "tcgplayer": {"unit": "USD", "holofoil": {"lowPrice": 227.69, "marketPrice": 291.94}},
    },
}
# Real shape for base1-8: Cardmarket (EUR) only, no TCGplayer.
TCGDEX_MACHAMP = {
    "id": "base1-8", "localId": "8", "name": "Machamp", "set": {"id": "base1", "name": "Base Set"},
    "pricing": {"cardmarket": {"unit": "EUR", "avg": 14.02, "trend": 11.26}, "tcgplayer": None},
}


async def no_sleep(_s: float) -> None:
    pass


def tcgdex(routes: dict[str, dict]) -> tuple[TCGdexSource, list[str]]:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        body = routes.get(request.url.path)
        return httpx.Response(200, json=body) if body else httpx.Response(404, json={})

    return TCGdexSource(transport=httpx.MockTransport(handler), sleep=no_sleep), seen


# --- id translation ----------------------------------------------------------

@pytest.mark.parametrize("pokemontcg_id, expected", [
    ("base1-8", "base1-8"),
    ("sv3pt5-199", "sv03.5-199"),
    ("sv3pt5-6", "sv03.5-006"),
    ("swsh12pt5-160", "swsh12.5-160"),
    ("sv1-25", "sv01-025"),
])
def test_candidate_ids_include_tcgdex_format(pokemontcg_id, expected):
    assert expected in candidate_ids(pokemontcg_id)
    assert candidate_ids(pokemontcg_id)[0] == pokemontcg_id  # cheapest guess first


def test_candidate_ids_does_not_mangle_double_digit_sv_sets():
    assert "sv010-1" not in candidate_ids("sv10-1")


# --- TCGdex parsing ----------------------------------------------------------

async def test_tcgdex_translates_id_and_keeps_original():
    src, seen = tcgdex({"/v2/en/cards/sv03.5-199": TCGDEX_CHARIZARD})
    card = await src.get_card("sv3pt5-199")
    assert card.id == "sv3pt5-199"  # database keeps the pokemontcg.io id
    assert card.market_price == 291.94
    assert card.price_source == "tcgdex/tcgplayer"
    assert card.image_url == "https://assets.tcgdex.net/en/sv/sv03.5/199/high.png"
    assert seen[-1] == "/v2/en/cards/sv03.5-199"


async def test_tcgdex_ignores_eur_cardmarket_prices():
    src, _ = tcgdex({"/v2/en/cards/base1-8": TCGDEX_MACHAMP})
    assert (await src.get_card("base1-8")).market_price is None


async def test_tcgdex_rejects_non_usd_tcgplayer_block():
    raw = {**TCGDEX_CHARIZARD, "pricing": {"tcgplayer": {"unit": "EUR", "holofoil": {"marketPrice": 1.0}}}}
    src, _ = tcgdex({"/v2/en/cards/sv03.5-199": raw})
    assert (await src.get_card("sv3pt5-199")).market_price is None


async def test_tcgdex_normalises_variant_names():
    raw = {**TCGDEX_CHARIZARD, "pricing": {"tcgplayer": {
        "unit": "USD", "reverse-holofoil": {"marketPrice": 5.0}, "normal": {"marketPrice": 2.5}}}}
    src, _ = tcgdex({"/v2/en/cards/sv03.5-199": raw})
    assert (await src.get_card("sv3pt5-199")).market_price == 2.5  # "normal" preferred


async def test_tcgdex_unknown_card():
    src, seen = tcgdex({})
    with pytest.raises(CardNotFoundError):
        await src.get_card("sv3pt5-199")
    assert len(seen) == len(candidate_ids("sv3pt5-199"))  # tried every candidate, then stopped


async def test_tcgdex_is_price_only():
    src, _ = tcgdex({})
    with pytest.raises(SourceError):
        await src.search_cards("Charizard")
    with pytest.raises(SourceError):
        await src.get_set("151")


# --- failover rules ----------------------------------------------------------

class DownSource(FakePriceSource):
    """Primary that is 'down': every call raises SourceUnavailableError."""

    name = "primary"

    async def get_card(self, card_id):
        self.calls.append(("get_card", card_id))
        raise SourceUnavailableError("primary unavailable: server error (500)")


class Backup(FakePriceSource):
    name = "backup"

    async def get_card(self, card_id):
        card = await super().get_card(card_id)
        import dataclasses
        return dataclasses.replace(card, set_name="Backup Naming", price_source="backup")


def fallback(primary: PriceSource, backup: PriceSource, now: list[float]) -> FallbackPriceSource:
    return FallbackPriceSource(primary, backup, cooldown_seconds=300, clock=lambda: now[0])


async def test_uses_backup_when_primary_down():
    primary, backup = DownSource(), Backup()
    backup.add(make_card("base1-4", price=300.0))
    src = fallback(primary, backup, [0.0])
    card = await src.get_card("base1-4")
    assert card.market_price == 300.0 and card.price_source == "backup"


async def test_circuit_breaker_skips_primary_then_retries_it():
    primary, backup = DownSource(), Backup()
    backup.add(make_card("base1-4"))
    now = [0.0]
    src = fallback(primary, backup, now)
    await src.get_card("base1-4")
    await src.get_card("base1-4")
    assert len(primary.calls) == 1  # second call skipped the dead primary
    now[0] = 301
    await src.get_card("base1-4")
    assert len(primary.calls) == 2  # cool-down over: primary tried again


async def test_not_found_on_primary_does_not_fall_back():
    primary, backup = FakePriceSource(), Backup()
    backup.add(make_card("base1-4"))
    with pytest.raises(CardNotFoundError):
        await fallback(primary, backup, [0.0]).get_card("base1-4")
    assert backup.calls == []


async def test_backup_fills_missing_price_but_keeps_primary_metadata():
    primary, backup = FakePriceSource(), Backup()
    primary.add(make_card("base1-8", name="Machamp", price=None))
    backup.add(make_card("base1-8", name="Machamp", price=20.0))
    card = await fallback(primary, backup, [0.0]).get_card("base1-8")
    assert card.market_price == 20.0 and card.price_source == "backup"
    assert card.set_name == "Base"  # primary's naming kept


async def test_both_down_reports_primary_error():
    primary, backup = DownSource(), Backup()  # backup has no cards -> not found
    with pytest.raises(SourceUnavailableError, match="primary unavailable"):
        await fallback(primary, backup, [0.0]).get_card("base1-4")


async def test_search_and_sets_never_use_backup():
    primary, backup = FakePriceSource(), Backup()
    primary.add(make_card("base1-4"))
    src = fallback(primary, backup, [0.0])
    assert len(await src.search_cards("Charizard")) == 1
    assert backup.calls == []


# --- provenance through the service ------------------------------------------

async def test_snapshot_records_backup_origin_and_keeps_metadata(db, clock):
    primary, backup = FakePriceSource(), Backup()
    primary.name = "primary"
    primary.add(make_card("base1-4", price=100.0))
    backup.add(make_card("base1-4", price=120.0))
    now = [0.0]
    service = CollectionService(db, fallback(primary, backup, now), today=clock)
    await service.add_card("base1-4")  # primary up

    async def down(card_id):
        raise SourceUnavailableError("primary unavailable")

    primary.get_card = down  # primary goes down
    from datetime import timedelta
    clock.value += timedelta(days=1)
    out = await service.snapshot_prices()
    assert out["prices"] == [{"card_id": "base1-4", "price": 120.0, "source": "backup"}]

    hist = await service.price_history("base1-4", 30)
    assert [p["source"] for p in hist["points"]] == ["primary", "backup"]
    assert db.get_card("base1-4").set_name == "Base"  # backup naming didn't overwrite
