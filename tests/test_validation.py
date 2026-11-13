import math

import pytest

from pokemon_mcp.models import (
    Condition, ValidationError, validate_card_id, validate_condition, validate_days,
    validate_name, validate_optional_name, validate_price, validate_quantity,
)


@pytest.mark.parametrize("good", ["base1-4", "swsh12pt5-160", "sv3pt5-199", "  xy7-54 "])
def test_card_id_accepts_real_ids(good):
    assert validate_card_id(good) == good.strip()


@pytest.mark.parametrize("bad", [
    "", "-base1", "base1-4'; DROP TABLE cards;--", "../../etc/passwd", "a" * 41,
    "base1 4", "base1/4", "base1-4?x=1", 123, None,
])
def test_card_id_rejects_bad_input(bad):
    with pytest.raises(ValidationError):
        validate_card_id(bad)


@pytest.mark.parametrize("good", ["Charizard", "Farfetch'd", "Mr. Mime", "Nidoran♀",
                                  "Pokémon Trainer: Red & Blue", "Scarlet & Violet 151"])
def test_name_accepts_real_names(good):
    assert validate_name(good) == good


def test_name_collapses_whitespace():
    assert validate_name("  Charizard   ex ") == "Charizard ex"


@pytest.mark.parametrize("bad", [
    "", "   ", 'Charizard" OR name:"*', "char*", "a\\b", "x" * 101, "name:(a)", None, 5,
])
def test_name_rejects_query_syntax_and_junk(bad):
    with pytest.raises(ValidationError):
        validate_name(bad)


def test_optional_name():
    assert validate_optional_name(None, "set_name") is None
    assert validate_optional_name("  ", "set_name") is None
    assert validate_optional_name("Base", "set_name") == "Base"


def test_condition_is_case_insensitive():
    assert validate_condition("nm") is Condition.NM
    with pytest.raises(ValidationError):
        validate_condition("MINT")


@pytest.mark.parametrize("bad", [0, -1, 10_001, 1.5, True, "3"])
def test_quantity_bounds(bad):
    with pytest.raises(ValidationError):
        validate_quantity(bad)


def test_price():
    assert validate_price(None) is None
    assert validate_price(12.345) == 12.35
    for bad in (-0.01, 1_000_001, math.nan, math.inf, "10", True):
        with pytest.raises(ValidationError):
            validate_price(bad)


@pytest.mark.parametrize("bad", [0, 3651, -5, 2.0, False])
def test_days_bounds(bad):
    with pytest.raises(ValidationError):
        validate_days(bad)
