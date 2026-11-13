"""Shared fixtures. No test ever touches the real network."""

from __future__ import annotations

from datetime import date

import pytest

from pokemon_mcp.db import Database
from pokemon_mcp.models import Card, SetInfo
from pokemon_mcp.service import CollectionService
from pokemon_mcp.sources.base import CardNotFoundError, PriceSource


def make_card(card_id: str, name: str = "Charizard", price: float | None = 100.0,
              set_id: str = "base1", set_name: str = "Base", number: str = "4") -> Card:
    return Card(id=card_id, name=name, set_id=set_id, set_name=set_name, number=number,
                rarity="Rare Holo", image_url=f"https://images.example/{card_id}.png",
                market_price=price)


class FakePriceSource(PriceSource):
    """In-memory stand-in for pokemontcg.io used by service/tool tests."""

    name = "fake"

    def __init__(self) -> None:
        self.cards: dict[str, Card] = {}
        self.sets: dict[str, SetInfo] = {}
        self.calls: list[tuple] = []

    def add(self, card: Card) -> Card:
        self.cards[card.id] = card
        return card

    async def search_cards(self, name, set_name=None, limit=20):
        self.calls.append(("search", name, set_name))
        return [c for c in self.cards.values()
                if name.lower() in c.name.lower()
                and (set_name is None or set_name.lower() == c.set_name.lower())][:limit]

    async def get_card(self, card_id):
        self.calls.append(("get_card", card_id))
        if card_id not in self.cards:
            raise CardNotFoundError(f"Card '{card_id}' was not found.")
        return self.cards[card_id]

    async def get_set(self, set_name):
        self.calls.append(("get_set", set_name))
        if set_name.lower() not in self.sets:
            raise CardNotFoundError(f"Set '{set_name}' was not found.")
        return self.sets[set_name.lower()]


class Clock:
    """Controllable 'today' for date-dependent logic."""

    def __init__(self, today: date) -> None:
        self.value = today

    def __call__(self) -> date:
        return self.value


@pytest.fixture
def db():
    database = Database(":memory:")
    yield database
    database.close()


@pytest.fixture
def source() -> FakePriceSource:
    return FakePriceSource()


@pytest.fixture
def clock() -> Clock:
    return Clock(date(2026, 1, 15))


@pytest.fixture
def service(db, source, clock) -> CollectionService:
    return CollectionService(db, source, today=clock)
