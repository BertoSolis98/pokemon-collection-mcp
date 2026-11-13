"""Tests for the pokemontcg.io client using httpx.MockTransport (no network)."""

from __future__ import annotations

import httpx
import pytest

from pokemon_mcp.sources.base import CardNotFoundError, SourceError, SourceUnavailableError
from pokemon_mcp.sources.cache import TTLCache
from pokemon_mcp.sources.pokemontcg import PokemonTCGSource

CHARIZARD = {
    "id": "base1-4", "name": "Charizard", "number": "4", "rarity": "Rare Holo",
    "set": {"id": "base1", "name": "Base"},
    "images": {"small": "https://img/s.png", "large": "https://img/l.png"},
    "tcgplayer": {"prices": {"holofoil": {"low": 200.0, "market": 350.5}}},
}


class Recorder:
    """Mock HTTP handler that replays a list of responses and records requests."""

    def __init__(self, *responses: httpx.Response | Exception):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(r, Exception):
            raise r
        return r


async def no_sleep(_seconds: float) -> None:
    pass


def make_source(handler, api_key="test-key", **kw) -> PokemonTCGSource:
    kw.setdefault("sleep", no_sleep)
    return PokemonTCGSource(api_key, transport=httpx.MockTransport(handler), **kw)


async def test_get_card_parses_price_and_sends_key():
    rec = Recorder(httpx.Response(200, json={"data": CHARIZARD}))
    card = await make_source(rec).get_card("base1-4")
    assert card.market_price == 350.5
    assert card.set_name == "Base" and card.image_url == "https://img/l.png"
    assert rec.requests[0].headers["X-Api-Key"] == "test-key"
    assert rec.requests[0].url.path == "/v2/cards/base1-4"


async def test_no_key_header_when_key_missing():
    rec = Recorder(httpx.Response(200, json={"data": CHARIZARD}))
    await make_source(rec, api_key=None).get_card("base1-4")
    assert "X-Api-Key" not in rec.requests[0].headers


async def test_card_without_prices_has_none():
    raw = {**CHARIZARD, "tcgplayer": None}
    rec = Recorder(httpx.Response(200, json={"data": raw}))
    assert (await make_source(rec).get_card("base1-4")).market_price is None


async def test_search_builds_quoted_query():
    rec = Recorder(httpx.Response(200, json={"data": [CHARIZARD]}))
    results = await make_source(rec).search_cards("Charizard", "Base")
    assert [c.id for c in results] == ["base1-4"]
    assert rec.requests[0].url.params["q"] == 'name:"Charizard" set.name:"Base"'


async def test_404_is_card_not_found():
    rec = Recorder(httpx.Response(404, json={"error": "not found"}))
    with pytest.raises(CardNotFoundError, match="nope-1"):
        await make_source(rec).get_card("nope-1")


async def test_429_is_retried_honouring_retry_after():
    slept: list[float] = []

    async def fake_sleep(s):
        slept.append(s)

    rec = Recorder(
        httpx.Response(429, headers={"Retry-After": "2"}),
        httpx.Response(200, json={"data": CHARIZARD}),
    )
    card = await make_source(rec, sleep=fake_sleep).get_card("base1-4")
    assert card.id == "base1-4"
    assert slept == [2.0]
    assert len(rec.requests) == 2


async def test_retry_after_is_capped():
    slept: list[float] = []

    async def fake_sleep(s):
        slept.append(s)

    rec = Recorder(httpx.Response(429, headers={"Retry-After": "9999"}),
                   httpx.Response(200, json={"data": CHARIZARD}))
    await make_source(rec, sleep=fake_sleep).get_card("base1-4")
    assert slept == [30.0]


async def test_gives_up_after_max_retries_on_5xx():
    rec = Recorder(httpx.Response(503))
    with pytest.raises(SourceUnavailableError, match="server error"):
        await make_source(rec, max_retries=2).get_card("base1-4")
    assert len(rec.requests) == 3  # first try + 2 retries


async def test_timeout_is_retried_then_reported():
    rec = Recorder(httpx.ReadTimeout("slow"))
    with pytest.raises(SourceUnavailableError, match="timed out"):
        await make_source(rec, max_retries=1).get_card("base1-4")


async def test_auth_error_does_not_leak_key():
    rec = Recorder(httpx.Response(403, text="bad key: test-key"))
    with pytest.raises(SourceError) as exc:
        await make_source(rec).get_card("base1-4")
    assert "test-key" not in str(exc.value)
    assert len(rec.requests) == 1  # auth errors are not retried


async def test_responses_are_cached():
    rec = Recorder(httpx.Response(200, json={"data": CHARIZARD}))
    src = make_source(rec)
    await src.get_card("base1-4")
    await src.get_card("base1-4")
    assert len(rec.requests) == 1


async def test_cache_expires():
    now = [0.0]
    cache = TTLCache(ttl_seconds=60, clock=lambda: now[0])
    rec = Recorder(httpx.Response(200, json={"data": CHARIZARD}))
    src = make_source(rec, cache=cache)
    await src.get_card("base1-4")
    now[0] = 61
    await src.get_card("base1-4")
    assert len(rec.requests) == 2


async def test_get_set_paginates():
    def card(n):
        return {**CHARIZARD, "id": f"base1-{n}", "number": str(n)}

    rec = Recorder(
        httpx.Response(200, json={"data": [{"id": "base1", "name": "Base"},
                                           {"id": "base2", "name": "Base Set 2"}]}),
        httpx.Response(200, json={"data": [card(1), card(2)], "totalCount": 3}),
        httpx.Response(200, json={"data": [card(3)], "totalCount": 3}),
    )
    info = await make_source(rec).get_set("base")
    assert info.id == "base1"
    assert [c.id for c in info.cards] == ["base1-1", "base1-2", "base1-3"]


async def test_unknown_set():
    rec = Recorder(httpx.Response(200, json={"data": []}))
    with pytest.raises(CardNotFoundError):
        await make_source(rec).get_set("Nope")


def test_ttl_cache_is_bounded():
    cache = TTLCache(max_entries=2)
    for k in "abc":
        cache.set(k, k)
    assert cache.get("a") is None and cache.get("c") == "c"
