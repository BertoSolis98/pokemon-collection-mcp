"""Runtime settings, loaded from environment variables / a local .env file.

Secrets are never hardcoded. The API key is read from the environment and is
excluded from repr() so it can't leak into logs or error messages by accident.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_DB_PATH = Path.home() / ".pokemon-mcp" / "collection.db"


@dataclass(frozen=True)
class Settings:
    # repr=False keeps the key out of any accidental print/log of Settings.
    api_key: str | None = field(repr=False)
    db_path: Path
    cache_ttl_seconds: float = 300.0
    http_timeout_seconds: float = 10.0
    max_retries: int = 3


def load_settings() -> Settings:
    """Read settings from the environment (after loading .env, if present)."""
    # override=False: real environment variables win over the .env file.
    load_dotenv(override=False)

    api_key = os.getenv("POKEMONTCG_API_KEY", "").strip() or None
    db_path_raw = os.getenv("POKEMON_MCP_DB_PATH", "").strip()
    db_path = Path(db_path_raw).expanduser() if db_path_raw else DEFAULT_DB_PATH

    return Settings(api_key=api_key, db_path=db_path)
