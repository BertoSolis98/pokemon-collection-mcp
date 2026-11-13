"""Business logic behind the MCP tools.

server.py only handles the MCP protocol; this module holds what each tool
actually does. Keeping them apart means the logic can be tested without an
MCP client, and the tools stay a thin, easy-to-audit layer.

Every public method validates its own inputs first (see models.py).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

from .db import CollectionError, Database
from .models import (
    Card,
    validate_card_id,
    validate_condition,
    validate_days,
    validate_name,
    validate_optional_name,
    validate_price,
    validate_quantity,
    ValidationError,
)
from .sources.base import PriceSource, SourceError

MAX_SEARCH_RESULTS = 20
MAX_MOVERS = 50


class CollectionService:
    def __init__(
        self,
        db: Database,
        source: PriceSource,
        today: Callable[[], date] = date.today,
    ) -> None:
        self.db = db
        self.source = source
        self._today = today  # injectable so tests can control "today"

    # --- READ-ONLY ---------------------------------------------------------

    async def search_card(self, name: str, set_name: str | None = None) -> dict[str, Any]:
        name = validate_name(name, "name")
        set_name = validate_optional_name(set_name, "set_name")
        cards = await self.source.search_cards(name, set_name, limit=MAX_SEARCH_RESULTS)
        return {
            "query": {"name": name, "set": set_name},
            "count": len(cards),
            "results": [_card_dict(c) for c in cards],
        }

    async def collection_value(self) -> dict[str, Any]:
        holdings = self.db.holdings()
        lines: list[dict[str, Any]] = []
        total_value = cost_basis = costed_value = 0.0
        unpriced: list[str] = []
        unknown_cost_copies = 0

        for h in holdings:
            price, as_of = await self._current_price(h.card_id)
            if price is None:
                unpriced.append(h.card_id)
            value = round((price or 0.0) * h.quantity, 2)
            total_value += value
            cost_basis += h.known_cost
            unknown_cost_copies += h.unknown_cost_quantity
            # Gain/loss only counts copies whose purchase price we know.
            costed_value += (price or 0.0) * (h.quantity - h.unknown_cost_quantity)
            lines.append({
                "card_id": h.card_id, "name": h.name, "set": h.set_name,
                "quantity": h.quantity, "unit_price": price, "price_as_of": as_of,
                "value": value, "cost_basis": h.known_cost,
            })

        gain = round(costed_value - cost_basis, 2)
        result: dict[str, Any] = {
            "total_value": round(total_value, 2),
            "cost_basis": round(cost_basis, 2),
            "gain_loss": gain,
            "gain_loss_pct": round(gain / cost_basis * 100, 2) if cost_basis else None,
            "currency": "USD",
            "distinct_cards": len(holdings),
            "total_copies": sum(h.quantity for h in holdings),
            "cards": sorted(lines, key=lambda x: x["value"], reverse=True),
        }
        notes = []
        if unknown_cost_copies:
            notes.append(
                f"{unknown_cost_copies} copies have no purchase price and are excluded from gain/loss."
            )
        if unpriced:
            notes.append(f"No price available for: {', '.join(unpriced)}.")
        if notes:
            result["notes"] = notes
        return result

    async def price_history(self, card_id: str, days: int = 30) -> dict[str, Any]:
        card_id = validate_card_id(card_id)
        days = validate_days(days)
        since = self._today() - timedelta(days=days)
        snaps = self.db.snapshots_since(card_id, since)
        card = self.db.get_card(card_id)
        result: dict[str, Any] = {
            "card_id": card_id,
            "name": card.name if card else None,
            "days": days,
            "points": [
                {"date": s.captured_date.isoformat(), "price": s.price, "source": s.source}
                for s in snaps
            ],
        }
        if not snaps:
            result["note"] = (
                "No saved prices in this window. Run snapshot_prices daily to build history."
            )
            return result
        prices = [s.price for s in snaps]
        first, last = prices[0], prices[-1]
        result.update({
            "first": first, "last": last, "low": min(prices), "high": max(prices),
            "change": round(last - first, 2),
            "change_pct": round((last - first) / first * 100, 2) if first else None,
        })
        return result

    async def top_movers(self, days: int = 7, limit: int = 5) -> dict[str, Any]:
        days = validate_days(days)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_MOVERS:
            raise ValidationError(f"limit must be a whole number between 1 and {MAX_MOVERS}.")
        cutoff = self._today() - timedelta(days=days)

        movers = []
        for h in self.db.holdings():
            end = self.db.latest_snapshot(h.card_id)
            start = self.db.baseline_snapshot(h.card_id, cutoff)
            if not end or not start or start.captured_date >= end.captured_date:
                continue  # need two different days to measure a move
            change = round(end.price - start.price, 2)
            movers.append({
                "card_id": h.card_id, "name": h.name, "set": h.set_name,
                "from_date": start.captured_date.isoformat(), "from_price": start.price,
                "to_date": end.captured_date.isoformat(), "to_price": end.price,
                "change": change,
                "change_pct": round(change / start.price * 100, 2) if start.price else None,
                "position_change": round(change * h.quantity, 2),
            })

        def pct(m: dict[str, Any]) -> float:
            return m["change_pct"] if m["change_pct"] is not None else 0.0

        gainers = sorted((m for m in movers if m["change"] > 0), key=pct, reverse=True)
        losers = sorted((m for m in movers if m["change"] < 0), key=pct)
        result: dict[str, Any] = {
            "days": days, "gainers": gainers[:limit], "losers": losers[:limit],
            "cards_compared": len(movers),
        }
        if not movers:
            result["note"] = "Not enough price history yet. Run snapshot_prices on different days."
        return result

    async def set_progress(self, set_name: str) -> dict[str, Any]:
        set_name = validate_name(set_name, "set_name")
        info = await self.source.get_set(set_name)
        owned = self.db.owned_card_ids()
        set_ids = {c.id for c in info.cards}
        missing = sorted((c for c in info.cards if c.id not in owned), key=_number_key)
        owned_count = len(set_ids & owned)
        total = len(set_ids)
        return {
            "set": info.name, "set_id": info.id,
            "owned": owned_count, "total": total,
            "percent_complete": round(owned_count / total * 100, 1) if total else 0.0,
            "missing_count": len(missing),
            "missing": [
                {"card_id": c.id, "number": c.number, "name": c.name, "rarity": c.rarity,
                 "market_price": c.market_price}
                for c in missing
            ],
        }

    # --- WRITE -------------------------------------------------------------

    async def add_card(
        self, card_id: str, condition: str = "NM", quantity: int = 1,
        purchase_price: float | None = None,
    ) -> dict[str, Any]:
        card_id = validate_card_id(card_id)
        cond = validate_condition(condition)
        quantity = validate_quantity(quantity)
        price = validate_price(purchase_price)

        card = await self.source.get_card(card_id)  # confirms the card exists
        self._store_card(card)
        today = self._today()
        owned = self.db.add_lot(card.id, cond, quantity, price, today)
        if card.market_price is not None:
            self.db.save_snapshot(card.id, card.market_price, _origin(card, self.source), today)
        return {
            "added": quantity, "card": _card_dict(card), "condition": cond.value,
            "purchase_price_each": price, "total_owned": owned,
        }

    async def remove_card(
        self, card_id: str, quantity: int = 1, condition: str | None = None
    ) -> dict[str, Any]:
        card_id = validate_card_id(card_id)
        quantity = validate_quantity(quantity)
        cond = validate_condition(condition) if condition else None
        remaining = self.db.remove_copies(card_id, quantity, cond)
        return {"removed": quantity, "card_id": card_id, "remaining": remaining}

    async def snapshot_prices(self) -> dict[str, Any]:
        today = self._today()
        saved, no_price, failed = [], [], []
        for card_id in sorted(self.db.owned_card_ids()):
            try:
                card = await self.source.get_card(card_id)
            except SourceError as exc:
                failed.append({"card_id": card_id, "error": str(exc)})
                continue  # one bad card shouldn't stop the whole run
            self._store_card(card)
            if card.market_price is None:
                no_price.append(card_id)
                continue
            origin = _origin(card, self.source)
            self.db.save_snapshot(card.id, card.market_price, origin, today)
            saved.append({"card_id": card.id, "price": card.market_price, "source": origin})
        return {
            "date": today.isoformat(),
            "saved": len(saved), "prices": saved, "no_price": no_price, "failed": failed,
        }

    # --- helpers -----------------------------------------------------------

    def _store_card(self, card: Card) -> None:
        """Save card metadata, but never let a backup provider's naming
        ("Base Set" vs "Base", "sv03.5" vs "sv3pt5") overwrite what the
        primary source already stored. Backup answers only contribute prices."""
        from_backup = card.price_source is not None and card.price_source != self.source.name
        if from_backup and self.db.get_card(card.id) is not None:
            return
        self.db.upsert_card(card)

    async def _current_price(self, card_id: str) -> tuple[float | None, str | None]:
        """Latest saved snapshot; if none exists, ask the source (without saving)."""
        snap = self.db.latest_snapshot(card_id)
        if snap is not None:
            return snap.price, snap.captured_date.isoformat()
        try:
            card = await self.source.get_card(card_id)
        except SourceError:
            return None, None
        return card.market_price, (self._today().isoformat() if card.market_price is not None else None)


def _origin(card: Card, source: PriceSource) -> str:
    return card.price_source or source.name


def _card_dict(c: Card) -> dict[str, Any]:
    return {
        "card_id": c.id, "name": c.name, "set": c.set_name, "set_id": c.set_id,
        "number": c.number, "rarity": c.rarity, "image_url": c.image_url,
        "market_price_usd": c.market_price,
    }


def _number_key(card: Card) -> tuple[int, str]:
    """Sort '2' before '10', and promos like 'SV001' sensibly."""
    m = re.search(r"\d+", card.number)
    return (int(m.group()) if m else 10**9, card.number)


__all__ = ["CollectionService", "CollectionError"]
