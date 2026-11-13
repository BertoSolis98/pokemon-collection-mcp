"""PriceSource implementation backed by the pokemontcg.io v2 API.

Card data comes from pokemontcg.io; the price is the TCGplayer USD "market"
price that pokemontcg.io embeds in each card (raw / ungraded copies).

Robustness features:
  * every request has a timeout (short to connect, longer to read, because
    pokemontcg.io can be slow to answer large set pages);
  * timeouts, network errors, 429 (rate limited) and 5xx responses are
    retried with exponential backoff plus jitter, honouring the server's
    Retry-After header (capped), within an overall time budget so a tool
    call never hangs for minutes;
  * a set whose card list comes back empty or incomplete is treated as a
    failure, never reported as "0 of 0 owned";
  * successful responses are cached for a few minutes;
  * all failures are turned into SourceError subclasses with safe messages.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from ..models import Card, SetInfo
from .base import CardNotFoundError, PriceSource, SourceError, SourceUnavailableError
from .cache import TTLCache

log = logging.getLogger(__name__)

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

MAX_RETRY_AFTER_SECONDS = 30.0
# Total time we are willing to spend sleeping between retries for one request.
RETRY_BUDGET_SECONDS = 45.0
CONNECT_TIMEOUT_SECONDS = 5.0
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
        headers = {"Accept": "application/json", "User-Agent": "pokemon-collection-mcp/0.1"}
        if api_key:
            headers["X-Api-Key"] = api_key
        # `transport` lets tests plug in httpx.MockTransport: no real network.
        self._client = httpx.AsyncClient(
            base_url=BASE_URL,
            headers=headers,
            timeout=httpx.Timeout(timeout, connect=min(timeout, CONNECT_TIMEOUT_SECONDS)),
            transport=transport,
        )
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._retry_budget = retry_budget
        self._cache = cache if cache is not None else TTLCache()
        self._sleep = sleep

    async def aclose(self) -> None:
        await self._client.aclose()

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

    # --- HTTP plumbing -----------------------------------------------------

    async def _get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        cache_key = path + "?" + json.dumps(params or {}, sort_keys=True)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        last_problem = "unknown error"
        slept = 0.0
        attempts = 0
        for attempt in range(self._max_retries + 1):
            attempts += 1
            base = self._backoff_base * (2**attempt)
            # Jitter spreads retries out so they don't hammer a struggling API in lockstep.
            delay = base + random.uniform(0, base * 0.25)
            try:
                resp = await self._client.get(path, params=params)
            except httpx.TimeoutException:
                last_problem = "the request timed out"
            except httpx.TransportError:
                last_problem = "a network error occurred"
            else:
                if resp.status_code == 200:
                    try:
                        payload = resp.json()
                    except ValueError:
                        raise SourceError("The card API returned an invalid response.") from None
                    self._cache.set(cache_key, payload)
                    return payload
                if resp.status_code == 404:
                    raise CardNotFoundError("The requested card or set was not found.")
                if resp.status_code in (401, 403):
                    # Deliberately vague: never echo the key or the response body.
                    raise SourceError(
                        "The card API rejected the request. Check POKEMONTCG_API_KEY in your .env."
                    )
                if resp.status_code == 429:
                    last_problem = "the API rate limit was hit"
                    delay = _retry_after(resp, default=delay)
                elif resp.status_code >= 500:
                    last_problem = f"the API returned a server error ({resp.status_code})"
                else:
                    raise SourceError(f"The card API returned an unexpected status ({resp.status_code}).")

            if attempt == self._max_retries or slept + delay > self._retry_budget:
                break
            log.warning(
                "pokemontcg.io: %s (attempt %d); retrying in %.1fs", last_problem, attempts, delay
            )
            await self._sleep(delay)
            slept += delay

        raise SourceUnavailableError(
            f"Card API unavailable: {last_problem} (gave up after {attempts} attempts). "
            "Try again shortly."
        )


def _retry_after(resp: httpx.Response, default: float) -> float:
    raw = resp.headers.get("Retry-After")
    try:
        value = float(raw) if raw is not None else default
    except ValueError:
        value = default
    return max(0.0, min(value, MAX_RETRY_AFTER_SECONDS))


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
