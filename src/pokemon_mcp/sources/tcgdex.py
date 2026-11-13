"""PriceSource backed by the free TCGdex API (https://tcgdex.dev), used as a
*price* fallback when pokemontcg.io is down.

Only get_card is supported. Search and set listings stay on the primary
source so every card id in the database keeps one format. TCGdex uses a
different id scheme for many sets, so the pokemontcg.io id is translated
(see candidate_ids) and the returned Card keeps the ORIGINAL id.

Only TCGplayer prices in USD are used. TCGdex also carries Cardmarket
prices, but those are EUR from a different marketplace; mixing them into a
USD history would turn exchange-rate moves into fake "price changes".
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from ..models import Card, SetInfo
from .base import CardNotFoundError, PriceSource, SourceError
from .cache import TTLCache
from .http import RETRY_BUDGET_SECONDS, JsonHttpClient

BASE_URL = "https://api.tcgdex.net/v2/en"

# Same preference order as the primary source, compared after normalising
# keys (TCGdex may spell them "reverse-holofoil", "1st-edition-holofoil"...).
PRICE_VARIANTS = (
    "normal", "holofoil", "reverseholofoil",
    "1steditionholofoil", "1steditionnormal", "unlimitedholofoil",
)


def candidate_ids(card_id: str) -> list[str]:
    """Possible TCGdex ids for a pokemontcg.io id, most likely first.

    base1-8        -> base1-8                 (many older sets match as-is)
    sv3pt5-199     -> sv03.5-199              ("pt5" -> ".5", sv numbers 2-digit)
    sv3pt5-6       -> sv03.5-006              (TCGdex zero-pads card numbers)
    swsh12pt5-160  -> swsh12.5-160
    """
    set_part, _, number = card_id.rpartition("-")
    if not set_part or not number:
        return [card_id]

    sets = [set_part]
    translated = set_part.replace("pt5", ".5")
    translated = re.sub(r"^sv(\d)(?=\D|$)", r"sv0\1", translated)  # sv3 -> sv03
    sets.append(translated)

    numbers = [number]
    if number.isdigit():
        numbers.append(number.zfill(3))

    out: list[str] = []
    for s in sets:
        for n in numbers:
            cid = f"{s}-{n}"
            if cid not in out:
                out.append(cid)
    return out


class TCGdexSource(PriceSource):
    name = "tcgdex/tcgplayer"

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        max_retries: int = 2,
        backoff_base: float = 1.0,
        retry_budget: float = RETRY_BUDGET_SECONDS,
        cache: TTLCache | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._http = JsonHttpClient(
            BASE_URL, label="TCGdex", timeout=timeout, max_retries=max_retries,
            backoff_base=backoff_base, retry_budget=retry_budget, cache=cache,
            transport=transport, sleep=sleep,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def get_card(self, card_id: str) -> Card:
        for tcgdex_id in candidate_ids(card_id):
            try:
                raw = await self._http.get(f"/cards/{tcgdex_id}")
            except CardNotFoundError:
                continue
            if not isinstance(raw, dict) or not raw.get("id"):
                continue
            return _parse_card(raw, original_id=card_id)
        raise CardNotFoundError(f"Card '{card_id}' was not found on TCGdex.")

    async def search_cards(self, name: str, set_name: str | None = None, limit: int = 20) -> list[Card]:
        raise SourceError("TCGdex is only used as a price fallback; search uses pokemontcg.io.")

    async def get_set(self, set_name: str) -> SetInfo:
        raise SourceError("TCGdex is only used as a price fallback; set lists use pokemontcg.io.")


def _parse_card(raw: dict[str, Any], original_id: str) -> Card:
    set_info = raw.get("set") or {}
    image = raw.get("image")
    return Card(
        id=original_id,  # keep the id format already stored in the database
        name=str(raw.get("name", "")),
        set_id=str(set_info.get("id", "")),
        set_name=str(set_info.get("name", "")),
        number=str(raw.get("localId", "")),
        rarity=raw.get("rarity"),
        image_url=f"{image}/high.png" if isinstance(image, str) and image else None,
        market_price=_usd_market_price(raw),
        price_source=TCGdexSource.name,
    )


def _norm(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


def _usd_market_price(raw: dict[str, Any]) -> float | None:
    tcgplayer = (raw.get("pricing") or {}).get("tcgplayer")
    if not isinstance(tcgplayer, dict):
        return None
    if str(tcgplayer.get("unit", "USD")).upper() != "USD":
        return None  # never mix currencies into the USD history
    variants = {_norm(k): v for k, v in tcgplayer.items() if isinstance(v, dict)}
    ordered = [variants[k] for k in PRICE_VARIANTS if k in variants]
    ordered += [v for k, v in variants.items() if k not in PRICE_VARIANTS]
    for entry in ordered:
        market = entry.get("marketPrice")
        if isinstance(market, (int, float)) and not isinstance(market, bool) and market >= 0:
            return round(float(market), 2)
    return None
