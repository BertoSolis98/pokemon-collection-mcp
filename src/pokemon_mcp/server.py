"""The MCP server: exposes the collection tools to Claude.

Tools are grouped into two clearly separated sections:

  READ-ONLY tools  - never modify the database.
  WRITE tools      - add, remove or record data.

Each tool also carries MCP ToolAnnotations (readOnlyHint / destructiveHint),
so MCP clients like Claude Desktop can show or gate them accordingly.

Errors: expected problems (bad input, card not found, API down) come back as
short, friendly messages. Unexpected errors are logged to stderr and the
client only sees a generic message, so internals never leak.
"""

from __future__ import annotations

import functools
import logging
import sys
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from .config import load_settings
from .db import CollectionError, Database
from .models import ValidationError
from .service import CollectionService
from .sources.base import SourceError

log = logging.getLogger("pokemon_mcp")

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)
WRITE_DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)


def _safe(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """Turn known errors into clean tool errors; hide details of unknown ones."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except (ValidationError, CollectionError, SourceError) as exc:
            raise ToolError(str(exc)) from None
        except Exception:
            log.exception("Unexpected error in tool %s", fn.__name__)
            raise ToolError("Internal error. See the server log for details.") from None

    return wrapper


def build_server(service: CollectionService) -> FastMCP:
    mcp = FastMCP("pokemon-collection")

    # ======================================================================
    # READ-ONLY TOOLS — these never change the database.
    # ======================================================================

    @mcp.tool(annotations=READ_ONLY)
    @_safe
    async def search_card(
        name: Annotated[str, Field(description="Card name, e.g. 'Charizard'", max_length=100)],
        set_name: Annotated[str | None, Field(description="Optional set name, e.g. 'Base'", max_length=100)] = None,
    ) -> dict:
        """[READ-ONLY] Search pokemontcg.io for cards by name (and optional set).
        Returns card ids to use with add_card, plus the current TCGplayer market price."""
        return await service.search_card(name, set_name)

    @mcp.tool(annotations=READ_ONLY)
    @_safe
    async def collection_value() -> dict:
        """[READ-ONLY] Total market value of the collection, cost basis and gain/loss (USD).
        Uses the most recent saved price snapshot for each card."""
        return await service.collection_value()

    @mcp.tool(annotations=READ_ONLY)
    @_safe
    async def price_history(
        card_id: Annotated[str, Field(description="Card id, e.g. 'base1-4'", max_length=40)],
        days: Annotated[int, Field(description="How many days back (1-3650)")] = 30,
    ) -> dict:
        """[READ-ONLY] Saved daily prices for one card over the last N days."""
        return await service.price_history(card_id, days)

    @mcp.tool(annotations=READ_ONLY)
    @_safe
    async def top_movers(
        days: Annotated[int, Field(description="Window in days (1-3650)")] = 7,
        limit: Annotated[int, Field(description="Max gainers/losers to return (1-50)")] = 5,
    ) -> dict:
        """[READ-ONLY] Biggest price gainers and losers among cards you own over the last N days."""
        return await service.top_movers(days, limit)

    @mcp.tool(annotations=READ_ONLY)
    @_safe
    async def set_progress(
        set_name: Annotated[str, Field(description="Set name, e.g. 'Scarlet & Violet 151'", max_length=100)],
    ) -> dict:
        """[READ-ONLY] How much of a set you own, and which cards you're missing."""
        return await service.set_progress(set_name)

    # ======================================================================
    # WRITE TOOLS — these modify the database.
    # ======================================================================

    @mcp.tool(annotations=WRITE)
    @_safe
    async def add_card(
        card_id: Annotated[str, Field(description="Card id from search_card, e.g. 'base1-4'", max_length=40)],
        condition: Annotated[str, Field(description="NM, LP, MP, HP or DMG")] = "NM",
        quantity: Annotated[int, Field(description="Number of copies (1-10000)")] = 1,
        purchase_price: Annotated[float | None, Field(description="Price paid per copy in USD (optional)")] = None,
    ) -> dict:
        """[WRITE] Add copies of a card to your collection."""
        return await service.add_card(card_id, condition, quantity, purchase_price)

    @mcp.tool(annotations=WRITE_DESTRUCTIVE)
    @_safe
    async def remove_card(
        card_id: Annotated[str, Field(description="Card id, e.g. 'base1-4'", max_length=40)],
        quantity: Annotated[int, Field(description="Number of copies to remove (1-10000)")] = 1,
        condition: Annotated[str | None, Field(description="Only remove copies in this condition (optional)")] = None,
    ) -> dict:
        """[WRITE] Remove copies of a card from your collection (oldest purchases first)."""
        return await service.remove_card(card_id, quantity, condition)

    @mcp.tool(annotations=WRITE)
    @_safe
    async def snapshot_prices() -> dict:
        """[WRITE] Save today's market price for every card you own (one per card per day).
        Run this daily to build your own price history."""
        return await service.snapshot_prices()

    return mcp


def main() -> None:
    # stdio transport: stdout carries the MCP protocol, so logs go to stderr.
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from .sources.pokemontcg import PokemonTCGSource
    from .sources.cache import TTLCache

    settings = load_settings()
    if not settings.api_key:
        log.warning("POKEMONTCG_API_KEY is not set; using the API's lower anonymous rate limit.")
    db = Database(settings.db_path)
    source = PokemonTCGSource(
        settings.api_key,
        timeout=settings.http_timeout_seconds,
        max_retries=settings.max_retries,
        cache=TTLCache(settings.cache_ttl_seconds),
    )
    build_server(CollectionService(db, source)).run()


if __name__ == "__main__":
    main()
