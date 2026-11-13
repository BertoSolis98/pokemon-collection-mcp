"""Shared HTTP plumbing for every provider: one tested place for timeouts,
retries, backoff, caching and safe error messages.

  * every request has a timeout (short to connect, longer to read);
  * timeouts, network errors, 429 and 5xx responses are retried with
    exponential backoff plus jitter, honouring Retry-After (capped), within
    an overall time budget so a tool call never hangs for minutes;
  * only successful responses are cached, so a failure is never "remembered";
  * failures become SourceError subclasses whose messages never contain
    API keys or raw response bodies.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from .base import CardNotFoundError, SourceError, SourceUnavailableError
from .cache import TTLCache

log = logging.getLogger(__name__)

MAX_RETRY_AFTER_SECONDS = 30.0
RETRY_BUDGET_SECONDS = 45.0  # total sleep allowed between retries for one request
CONNECT_TIMEOUT_SECONDS = 5.0
USER_AGENT = "pokemon-collection-mcp/0.1"


class JsonHttpClient:
    def __init__(
        self,
        base_url: str,
        *,
        label: str,
        headers: dict[str, str] | None = None,
        auth_hint: str = "",
        timeout: float = 30.0,
        max_retries: int = 4,
        backoff_base: float = 1.0,
        retry_budget: float = RETRY_BUDGET_SECONDS,
        cache: TTLCache | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.label = label  # human-readable provider name used in messages/logs
        self._auth_hint = auth_hint
        # `transport` lets tests plug in httpx.MockTransport: no real network.
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"Accept": "application/json", "User-Agent": USER_AGENT, **(headers or {})},
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

    async def get(self, path: str, params: dict[str, str] | None = None) -> Any:
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
                        raise SourceError(f"{self.label} returned an invalid response.") from None
                    self._cache.set(cache_key, payload)
                    return payload
                if resp.status_code == 404:
                    raise CardNotFoundError("The requested card or set was not found.")
                if resp.status_code in (401, 403):
                    # Deliberately vague: never echo the key or the response body.
                    raise SourceError(f"{self.label} rejected the request. {self._auth_hint}".strip())
                if resp.status_code == 429:
                    last_problem = "the API rate limit was hit"
                    delay = _retry_after(resp, default=delay)
                elif resp.status_code >= 500:
                    last_problem = f"the API returned a server error ({resp.status_code})"
                else:
                    raise SourceError(
                        f"{self.label} returned an unexpected status ({resp.status_code})."
                    )

            if attempt == self._max_retries or slept + delay > self._retry_budget:
                break
            log.warning("%s: %s (attempt %d); retrying in %.1fs",
                        self.label, last_problem, attempts, delay)
            await self._sleep(delay)
            slept += delay

        raise SourceUnavailableError(
            f"{self.label} unavailable: {last_problem} (gave up after {attempts} attempts). "
            "Try again shortly."
        )


def _retry_after(resp: httpx.Response, default: float) -> float:
    raw = resp.headers.get("Retry-After")
    try:
        value = float(raw) if raw is not None else default
    except ValueError:
        value = default
    return max(0.0, min(value, MAX_RETRY_AFTER_SECONDS))
