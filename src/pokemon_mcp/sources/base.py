"""The PriceSource interface.

The MCP tools never talk to pokemontcg.io directly. They talk to this
interface, so a different provider (e.g. TCGdex) can be added by writing one
new class that implements these four methods. Nothing else has to change.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import Card, SetInfo


class SourceError(Exception):
    """Base class for provider failures. Messages are safe to show the user
    (they never contain API keys, URLs with credentials, or stack traces)."""


class CardNotFoundError(SourceError):
    """The requested card or set does not exist at the provider."""


class SourceUnavailableError(SourceError):
    """The provider is down, timing out, or rate-limiting us after retries."""


class PriceSource(ABC):
    #: Short identifier stored with each price snapshot, e.g. "pokemontcg.io/tcgplayer".
    name: str

    @abstractmethod
    async def search_cards(self, name: str, set_name: str | None = None, limit: int = 20) -> list[Card]:
        """Find cards by name, optionally restricted to a set name."""

    @abstractmethod
    async def get_card(self, card_id: str) -> Card:
        """Fetch one card (including its current market price).

        Raises CardNotFoundError if the id is unknown."""

    @abstractmethod
    async def get_set(self, set_name: str) -> SetInfo:
        """Fetch a set and its full card list. Raises CardNotFoundError if unknown."""

    async def aclose(self) -> None:  # pragma: no cover - optional hook
        """Release network resources. Default: nothing to do."""
