"""A PriceSource that wraps a primary and a backup provider.

  * search_cards / get_set: primary only, so card ids stay in one format.
  * get_card: primary first. If the primary is *unavailable* (down, timing
    out, rate limited after retries) the backup answers instead. If the
    primary works but has no price, the backup may fill in just the price.
    A genuine "card not found" from the primary is NOT retried elsewhere.
  * Circuit breaker: after the primary fails, skip it for a cool-down
    period and go straight to the backup, so e.g. snapshot_prices doesn't
    wait out the full retry budget once per card during an outage.
"""

from __future__ import annotations

import dataclasses
import logging
import time
from collections.abc import Callable

from ..models import Card, SetInfo
from .base import CardNotFoundError, PriceSource, SourceError, SourceUnavailableError

log = logging.getLogger(__name__)

DEFAULT_COOLDOWN_SECONDS = 300.0


class FallbackPriceSource(PriceSource):
    def __init__(
        self,
        primary: PriceSource,
        backup: PriceSource,
        *,
        cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.primary = primary
        self.backup = backup
        self.name = primary.name
        self._cooldown = cooldown_seconds
        self._clock = clock
        self._primary_down_until = 0.0

    @property
    def primary_is_skipped(self) -> bool:
        return self._clock() < self._primary_down_until

    async def aclose(self) -> None:
        await self.primary.aclose()
        await self.backup.aclose()

    async def search_cards(self, name: str, set_name: str | None = None, limit: int = 20) -> list[Card]:
        return await self.primary.search_cards(name, set_name, limit)

    async def get_set(self, set_name: str) -> SetInfo:
        return await self.primary.get_set(set_name)

    async def get_card(self, card_id: str) -> Card:
        primary_error: SourceUnavailableError | None = None
        if not self.primary_is_skipped:
            try:
                card = await self.primary.get_card(card_id)
            except SourceUnavailableError as exc:
                primary_error = exc
                self._primary_down_until = self._clock() + self._cooldown
                log.warning("%s unavailable; using %s for %.0fs",
                            self.primary.name, self.backup.name, self._cooldown)
            else:
                if card.market_price is not None:
                    return card
                return await self._fill_missing_price(card)

        try:
            card = await self.backup.get_card(card_id)
        except SourceError as backup_error:
            # Report the primary's problem first: it's the more useful message.
            if primary_error is not None:
                raise primary_error from None
            raise SourceUnavailableError(
                f"{self.primary.name} is temporarily skipped after recent failures and "
                f"the backup failed too: {backup_error}"
            ) from None
        return card

    async def _fill_missing_price(self, card: Card) -> Card:
        """Primary knows the card but has no price: ask the backup for one."""
        try:
            backup_card = await self.backup.get_card(card.id)
        except SourceError:
            return card
        if backup_card.market_price is None:
            return card
        # Keep the primary's metadata; take only the price (and its origin).
        return dataclasses.replace(
            card, market_price=backup_card.market_price, price_source=backup_card.price_source
        )


__all__ = ["FallbackPriceSource", "CardNotFoundError"]
