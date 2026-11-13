"""SQLite storage.

Three tables:
  cards            - card metadata cached from the API (name, set, image...)
  collection       - what you own. One row per purchase "lot", so each lot
                     keeps its own condition, purchase price and date added.
  price_snapshots  - one price per card per day, so you build your own
                     price history over time.

Security: every query uses "?" placeholders. User input is never formatted
into SQL text, so SQL injection is not possible through these functions.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .models import Card, Condition

SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    set_id      TEXT NOT NULL,
    set_name    TEXT NOT NULL,
    number      TEXT NOT NULL,
    rarity      TEXT,
    image_url   TEXT
);

CREATE TABLE IF NOT EXISTS collection (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id         TEXT NOT NULL REFERENCES cards(id),
    condition       TEXT NOT NULL CHECK (condition IN ('NM','LP','MP','HP','DMG')),
    quantity        INTEGER NOT NULL CHECK (quantity > 0),
    purchase_price  REAL CHECK (purchase_price IS NULL OR purchase_price >= 0),
    date_added      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_collection_card ON collection(card_id);

CREATE TABLE IF NOT EXISTS price_snapshots (
    card_id        TEXT NOT NULL REFERENCES cards(id),
    price          REAL NOT NULL CHECK (price >= 0),
    source         TEXT NOT NULL,
    captured_date  TEXT NOT NULL,
    PRIMARY KEY (card_id, captured_date)   -- one snapshot per card per day
);
"""


@dataclass(frozen=True)
class Holding:
    """All lots of one card, rolled up."""

    card_id: str
    name: str
    set_name: str
    quantity: int
    known_cost: float  # total paid for lots that have a purchase price
    unknown_cost_quantity: int  # copies added without a purchase price


@dataclass(frozen=True)
class Snapshot:
    card_id: str
    price: float
    source: str
    captured_date: date


class CollectionError(Exception):
    """A request that is valid in form but impossible (e.g. removing more than you own)."""


class Database:
    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            path = Path(path)
            # Owner-only directory: the DB holds personal collection data.
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._conn = sqlite3.connect(str(path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)
        if str(path) != ":memory:":
            try:
                os.chmod(path, 0o600)
            except OSError:  # pragma: no cover - e.g. unsupported filesystem
                pass

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        # Commits on success, rolls back on any exception.
        with self._conn:
            yield self._conn

    # --- cards -------------------------------------------------------------

    def upsert_card(self, card: Card) -> None:
        with self._transaction() as c:
            c.execute(
                """
                INSERT INTO cards (id, name, set_id, set_name, number, rarity, image_url)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name, set_id = excluded.set_id,
                    set_name = excluded.set_name, number = excluded.number,
                    rarity = excluded.rarity, image_url = excluded.image_url
                """,
                (card.id, card.name, card.set_id, card.set_name, card.number,
                 card.rarity, card.image_url),
            )

    def get_card(self, card_id: str) -> Card | None:
        row = self._conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
        if row is None:
            return None
        return Card(
            id=row["id"], name=row["name"], set_id=row["set_id"], set_name=row["set_name"],
            number=row["number"], rarity=row["rarity"], image_url=row["image_url"],
        )

    # --- collection --------------------------------------------------------

    def add_lot(
        self,
        card_id: str,
        condition: Condition,
        quantity: int,
        purchase_price: float | None,
        date_added: date,
    ) -> int:
        """Record a purchase. Returns the total number of copies now owned."""
        with self._transaction() as c:
            c.execute(
                """INSERT INTO collection (card_id, condition, quantity, purchase_price, date_added)
                   VALUES (?, ?, ?, ?, ?)""",
                (card_id, condition.value, quantity, purchase_price, date_added.isoformat()),
            )
        return self.owned_quantity(card_id)

    def owned_quantity(self, card_id: str, condition: Condition | None = None) -> int:
        sql = "SELECT COALESCE(SUM(quantity), 0) FROM collection WHERE card_id = ?"
        params: tuple = (card_id,)
        if condition is not None:
            sql += " AND condition = ?"
            params += (condition.value,)
        return int(self._conn.execute(sql, params).fetchone()[0])

    def remove_copies(self, card_id: str, quantity: int, condition: Condition | None = None) -> int:
        """Remove copies, oldest lot first (FIFO). Returns copies left of this card.

        Runs in one transaction: either all requested copies are removed or none.
        """
        sql = "SELECT id, quantity FROM collection WHERE card_id = ?"
        params: tuple = (card_id,)
        if condition is not None:
            sql += " AND condition = ?"
            params += (condition.value,)
        sql += " ORDER BY date_added, id"

        with self._transaction() as c:
            lots = c.execute(sql, params).fetchall()
            available = sum(lot["quantity"] for lot in lots)
            if available < quantity:
                what = f"{condition.value} copies" if condition else "copies"
                raise CollectionError(
                    f"You own {available} {what} of {card_id}; cannot remove {quantity}."
                )
            remaining = quantity
            for lot in lots:
                if remaining == 0:
                    break
                take = min(remaining, lot["quantity"])
                if take == lot["quantity"]:
                    c.execute("DELETE FROM collection WHERE id = ?", (lot["id"],))
                else:
                    c.execute(
                        "UPDATE collection SET quantity = quantity - ? WHERE id = ?",
                        (take, lot["id"]),
                    )
                remaining -= take
        return self.owned_quantity(card_id)

    def holdings(self) -> list[Holding]:
        rows = self._conn.execute(
            """
            SELECT c.id AS card_id, c.name, c.set_name,
                   SUM(l.quantity) AS quantity,
                   COALESCE(SUM(CASE WHEN l.purchase_price IS NOT NULL
                                     THEN l.purchase_price * l.quantity END), 0) AS known_cost,
                   COALESCE(SUM(CASE WHEN l.purchase_price IS NULL
                                     THEN l.quantity END), 0) AS unknown_qty
            FROM collection l JOIN cards c ON c.id = l.card_id
            GROUP BY c.id
            ORDER BY c.set_name, c.name
            """
        ).fetchall()
        return [
            Holding(
                card_id=r["card_id"], name=r["name"], set_name=r["set_name"],
                quantity=int(r["quantity"]), known_cost=round(float(r["known_cost"]), 2),
                unknown_cost_quantity=int(r["unknown_qty"]),
            )
            for r in rows
        ]

    def owned_card_ids(self) -> set[str]:
        return {r[0] for r in self._conn.execute("SELECT DISTINCT card_id FROM collection")}

    # --- price snapshots ---------------------------------------------------

    def save_snapshot(self, card_id: str, price: float, source: str, captured: date) -> None:
        """Insert today's price; re-running on the same day overwrites it."""
        with self._transaction() as c:
            c.execute(
                """
                INSERT INTO price_snapshots (card_id, price, source, captured_date)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(card_id, captured_date) DO UPDATE SET
                    price = excluded.price, source = excluded.source
                """,
                (card_id, price, source, captured.isoformat()),
            )

    def snapshots_since(self, card_id: str, since: date) -> list[Snapshot]:
        rows = self._conn.execute(
            """SELECT card_id, price, source, captured_date FROM price_snapshots
               WHERE card_id = ? AND captured_date >= ? ORDER BY captured_date""",
            (card_id, since.isoformat()),
        ).fetchall()
        return [_snapshot(r) for r in rows]

    def latest_snapshot(self, card_id: str) -> Snapshot | None:
        row = self._conn.execute(
            """SELECT card_id, price, source, captured_date FROM price_snapshots
               WHERE card_id = ? ORDER BY captured_date DESC LIMIT 1""",
            (card_id,),
        ).fetchone()
        return _snapshot(row) if row else None

    def baseline_snapshot(self, card_id: str, cutoff: date) -> Snapshot | None:
        """Price at the start of a window: the last snapshot on/before `cutoff`,
        or (if history is shorter than the window) the earliest one after it."""
        row = self._conn.execute(
            """SELECT card_id, price, source, captured_date FROM price_snapshots
               WHERE card_id = ? AND captured_date <= ?
               ORDER BY captured_date DESC LIMIT 1""",
            (card_id, cutoff.isoformat()),
        ).fetchone()
        if row is None:
            row = self._conn.execute(
                """SELECT card_id, price, source, captured_date FROM price_snapshots
                   WHERE card_id = ? ORDER BY captured_date ASC LIMIT 1""",
                (card_id,),
            ).fetchone()
        return _snapshot(row) if row else None


def _snapshot(row: sqlite3.Row) -> Snapshot:
    return Snapshot(
        card_id=row["card_id"], price=float(row["price"]), source=row["source"],
        captured_date=date.fromisoformat(row["captured_date"]),
    )
