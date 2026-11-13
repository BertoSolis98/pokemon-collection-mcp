import sqlite3
from datetime import date

import pytest

from pokemon_mcp.db import CollectionError, Database
from pokemon_mcp.models import Condition
from tests.conftest import make_card


def test_snapshot_is_one_per_card_per_day(db):
    db.upsert_card(make_card("base1-4"))
    db.save_snapshot("base1-4", 100.0, "fake", date(2026, 1, 1))
    db.save_snapshot("base1-4", 120.0, "fake", date(2026, 1, 1))  # same day: overwrite
    db.save_snapshot("base1-4", 130.0, "fake", date(2026, 1, 2))
    snaps = db.snapshots_since("base1-4", date(2025, 1, 1))
    assert [(s.captured_date.day, s.price) for s in snaps] == [(1, 120.0), (2, 130.0)]


def test_lots_and_fifo_removal(db):
    db.upsert_card(make_card("base1-4"))
    db.add_lot("base1-4", Condition.NM, 2, 50.0, date(2026, 1, 1))
    db.add_lot("base1-4", Condition.LP, 3, None, date(2026, 1, 5))
    assert db.owned_quantity("base1-4") == 5

    h = db.holdings()[0]
    assert (h.quantity, h.known_cost, h.unknown_cost_quantity) == (5, 100.0, 3)

    assert db.remove_copies("base1-4", 3) == 2  # removes the 2 NM, then 1 LP
    assert db.owned_quantity("base1-4", Condition.NM) == 0
    assert db.owned_quantity("base1-4", Condition.LP) == 2


def test_cannot_remove_more_than_owned_and_nothing_changes(db):
    db.upsert_card(make_card("base1-4"))
    db.add_lot("base1-4", Condition.NM, 2, 10.0, date(2026, 1, 1))
    with pytest.raises(CollectionError):
        db.remove_copies("base1-4", 3)
    with pytest.raises(CollectionError):
        db.remove_copies("base1-4", 1, Condition.LP)
    assert db.owned_quantity("base1-4") == 2


def test_hostile_strings_are_stored_as_data_not_executed(db):
    evil = "x'); DROP TABLE cards; --"
    db.upsert_card(make_card("base1-4", name=evil))
    assert db.get_card("base1-4").name == evil
    assert db.get_card(evil) is None
    # Table still exists and works.
    assert db.get_card("base1-4") is not None


def test_schema_constraints_reject_bad_rows(db):
    db.upsert_card(make_card("base1-4"))
    with pytest.raises(sqlite3.IntegrityError):
        db._conn.execute("INSERT INTO collection (card_id, condition, quantity, date_added) "
                         "VALUES ('base1-4', 'MINT', 1, '2026-01-01')")
    with pytest.raises(sqlite3.IntegrityError):
        db._conn.execute("INSERT INTO collection (card_id, condition, quantity, date_added) "
                         "VALUES ('base1-4', 'NM', 0, '2026-01-01')")
    with pytest.raises(sqlite3.IntegrityError):  # foreign key: unknown card
        db._conn.execute("INSERT INTO collection (card_id, condition, quantity, date_added) "
                         "VALUES ('nope-1', 'NM', 1, '2026-01-01')")


def test_db_file_is_private(tmp_path):
    path = tmp_path / "sub" / "collection.db"
    Database(path).close()
    assert path.exists()
    assert (path.stat().st_mode & 0o077) == 0
