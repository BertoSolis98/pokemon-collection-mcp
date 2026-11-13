"""PriceSource implementation backed by the pokemontcg.io v2 API.

Card data comes from pokemontcg.io; the price is the TCGplayer USD "market"
price that pokemontcg.io embeds in each card (raw / ungraded copies).

Network robustness (timeouts, retries with backoff, caching, safe errors)
lives in http.py and is shared with the other providers. This module adds:
  * a set whose card list comes back empty or incomplete is treated as a
    failure, never reported as "0 of 0 owned".
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from ..models import Card, SetInfo
from .base import CardNotFoundError, PriceSource, SourceUnavailableError
from .cache import TTLCache
from .http import RETRY_BUDGET_SECONDS, JsonHttpClient

BASE_URL = "https://api.pokemontcg.io/v2"

# Which TCGplayer price variant to use, in order of preference.
PRICE_VARIANTS = (
    "normal",
    "holofoil",
    "reverseHolofoil",
    "1stEditionHolofoil",
    "1stEditionNormal",
    "unlimitedHolofoil",
)

SET_PAGE_SIZE = 250


class PokemonTCGSource(PriceSource):
    name = "pokemontcg.io/tcgplayer"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        timeout: float = 30.0,
        max_retries: int = 4,
        backoff_base: float = 1.0,
        retry_budget: float = RETRY_BUDGET_SECONDS,
        cache: TTLCache | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._http = JsonHttpClient(
            BASE_URL,
            label="pokemontcg.io",
            headers={"X-Api-Key": api_key} if api_key else None,
            auth_hint="Check POKEMONTCG_API_KEY in your .env.",
            timeout=timeout, max_retries=max_retries, backoff_base=backoff_base,
            retry_budget=retry_budget, cache=cache, transport=transport, sleep=sleep,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    # --- PriceSource API ---------------------------------------------------

    async def search_cards(self, name: str, set_name: str | None = None, limit: int = 20) -> list[Card]:
        # Inputs were validated upstream (no quotes/backslashes), so wrapping
        # them in quotes cannot break out of the query string.
        query = f'name:"{name}"'
        if set_name:
            query += f' set.name:"{set_name}"'
        data = await self._get(
            "/cards",
            {"q": query, "pageSize": str(limit), "orderBy": "-set.releaseDate,number"},
        )
        return [_parse_card(c) for c in data.get("data", [])]

    async def get_card(self, card_id: str) -> Card:
        try:
            data = await self._get(f"/cards/{card_id}")
        except CardNotFoundError:
            raise CardNotFoundError(f"Card '{card_id}' was not found.") from None
        if not data.get("data"):
            raise CardNotFoundError(f"Card '{card_id}' was not found.")
        return _parse_card(data["data"])

    async def get_set(self, set_name: str) -> SetInfo:
        sets = await self._get("/sets", {"q": f'name:"{set_name}"', "orderBy": "-releaseDate"})
        matches = sets.get("data", [])
        if not matches:
            raise CardNotFoundError(f"Set '{set_name}' was not found.")
        # Prefer an exact (case-insensitive) name match over a partial one.
        chosen = next(
            (s for s in matches if s.get("name", "").lower() == set_name.lower()), matches[0]
        )
        set_id = chosen["id"]

        cards: list[Card] = []
        page = 1
        while True:
            data = await self._get(
                "/cards",
                {"q": f"set.id:{set_id}", "page": str(page), "pageSize": str(SET_PAGE_SIZE)},
            )
            batch = data.get("data", [])
            cards.extend(_parse_card(c) for c in batch)
            total = int(data.get("totalCount", len(cards)))
            if len(cards) >= total:
                break
            if not batch:
                # The API said there are more cards but sent none: an
                # incomplete answer must not be reported as real progress.
                raise SourceUnavailableError(
                    f"The card API returned an incomplete card list for set '{chosen.get('name', set_name)}'. "
                    "Try again shortly."
                )
            page += 1
        if not cards:
            raise SourceUnavailableError(
                f"The card API returned no cards for set '{chosen.get('name', set_name)}'. Try again shortly."
            )
        return SetInfo(id=set_id, name=chosen.get("name", set_name), cards=cards)

    async def _get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        return await self._http.get(path, params)


def _parse_card(raw: dict[str, Any]) -> Card:
    set_info = raw.get("set") or {}
    images = raw.get("images") or {}
    return Card(
        id=str(raw["id"]),
        name=str(raw.get("name", "")),
        set_id=str(set_info.get("id", "")),
        set_name=str(set_info.get("name", "")),
        number=str(raw.get("number", "")),
        rarity=raw.get("rarity"),
        image_url=images.get("large") or images.get("small"),
        market_price=_market_price(raw),
    )


def _market_price(raw: dict[str, Any]) -> float | None:
    prices = (raw.get("tcgplayer") or {}).get("prices") or {}
    for variant in PRICE_VARIANTS:
        market = (prices.get(variant) or {}).get("market")
        if isinstance(market, (int, float)) and market >= 0:
            return round(float(market), 2)
    # Fall back to any other variant the API might add later.
    for entry in prices.values():
        market = (entry or {}).get("market")
        if isinstance(market, (int, float)) and market >= 0:
            return round(float(market), 2)
    return None
