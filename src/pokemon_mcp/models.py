"""Data models and input validation.

Every value that arrives from a tool call passes through one of the
validate_* functions below before it touches the database or the API.
The rule is "allow-list, not block-list": we describe what good input looks
like and reject everything else.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, field
from enum import StrEnum


class ValidationError(ValueError):
    """Raised when a tool argument is invalid. The message is safe to show."""


class Condition(StrEnum):
    """Standard raw-card conditions, best to worst."""

    NM = "NM"  # Near Mint
    LP = "LP"  # Lightly Played
    MP = "MP"  # Moderately Played
    HP = "HP"  # Heavily Played
    DMG = "DMG"  # Damaged


@dataclass(frozen=True)
class Card:
    """A card as returned by a PriceSource."""

    id: str
    name: str
    set_id: str
    set_name: str
    number: str
    rarity: str | None = None
    image_url: str | None = None
    # TCGplayer USD market price for a raw (ungraded) copy, if known.
    market_price: float | None = None
    # Which provider the price came from (set by fallback sources so price
    # history records the true origin of every number). None = this source.
    price_source: str | None = None


@dataclass(frozen=True)
class SetInfo:
    """A card set plus every card in it."""

    id: str
    name: str
    cards: list[Card] = field(default_factory=list)


# --- Limits -----------------------------------------------------------------

MAX_QUANTITY = 10_000
MAX_PRICE = 1_000_000.0
MAX_DAYS = 3650
MAX_NAME_LENGTH = 100

# pokemontcg.io ids look like "base1-4", "swsh12pt5-160", "sv3pt5-199".
_CARD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-]{0,39}$")

# Card / set names: letters (any language), digits, spaces and the punctuation
# that really appears in card names ("Farfetch'd", "Mr. Mime", "Nidoran♀",
# "Pokémon Trainer: Red & Blue"). Quotes, backslashes, wildcards and
# brackets are rejected because they have meaning in the API's query syntax.
_NAME_RE = re.compile(r"^[\w .,'’:&!?\-♀♂é]+$")


# --- Validators -------------------------------------------------------------


def validate_card_id(value: object) -> str:
    if not isinstance(value, str):
        raise ValidationError("card_id must be a string.")
    value = value.strip()
    if not _CARD_ID_RE.fullmatch(value):
        raise ValidationError(
            "card_id must look like 'base1-4' (letters, digits, '.' or '-', max 40 chars)."
        )
    return value


def validate_name(value: object, field_name: str = "name") -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field_name} must be a string.")
    # Normalise unicode and collapse whitespace so lookalike tricks don't slip in.
    value = " ".join(unicodedata.normalize("NFKC", value).split())
    if not value:
        raise ValidationError(f"{field_name} must not be empty.")
    if len(value) > MAX_NAME_LENGTH:
        raise ValidationError(f"{field_name} must be at most {MAX_NAME_LENGTH} characters.")
    if not _NAME_RE.fullmatch(value):
        raise ValidationError(
            f"{field_name} contains unsupported characters "
            "(quotes, backslashes, wildcards and brackets are not allowed)."
        )
    return value


def validate_optional_name(value: object, field_name: str) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return validate_name(value, field_name)


def validate_condition(value: object) -> Condition:
    if not isinstance(value, str):
        raise ValidationError("condition must be a string.")
    try:
        return Condition(value.strip().upper())
    except ValueError:
        allowed = ", ".join(c.value for c in Condition)
        raise ValidationError(f"condition must be one of: {allowed}.") from None


def validate_quantity(value: object) -> int:
    # bool is a subclass of int in Python; reject it explicitly.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError("quantity must be a whole number.")
    if not 1 <= value <= MAX_QUANTITY:
        raise ValidationError(f"quantity must be between 1 and {MAX_QUANTITY}.")
    return value


def validate_price(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError("purchase_price must be a number.")
    value = float(value)
    if math.isnan(value) or math.isinf(value):
        raise ValidationError("purchase_price must be a finite number.")
    if not 0 <= value <= MAX_PRICE:
        raise ValidationError(f"purchase_price must be between 0 and {MAX_PRICE:,.0f}.")
    return round(value, 2)


def validate_days(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError("days must be a whole number.")
    if not 1 <= value <= MAX_DAYS:
        raise ValidationError(f"days must be between 1 and {MAX_DAYS}.")
    return value
