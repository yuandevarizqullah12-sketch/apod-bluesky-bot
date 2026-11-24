"""Configuration from environment variables (names unchanged from the original bot)."""
import os
from dataclasses import dataclass
from typing import Optional

try:  # optional: only used for local runs with a .env file
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    bsky_handle: str = ""
    bsky_password: str = ""
    nasa_api_key: Optional[str] = None  # kept for compatibility; the new APOD API needs no key
    state_file: str = "data.json"
    flashback_start_year: int = 1995
    flashback_end_year: Optional[int] = None  # None = up to yesterday


def load_config(require_bluesky: bool = True) -> Config:
    if load_dotenv:
        load_dotenv()
    handle = os.getenv("BSKY_HANDLE", "").strip()
    password = os.getenv("BSKY_PASSWORD", "").strip()
    if require_bluesky and not (handle and password):
        raise ConfigError("BSKY_HANDLE and BSKY_PASSWORD must be set")
    end = os.getenv("FLASHBACK_END_YEAR")
    return Config(
        bsky_handle=handle,
        bsky_password=password,
        nasa_api_key=os.getenv("NASA_API_KEY") or None,
        state_file=os.getenv("APOD_STATE_FILE", "data.json"),
        flashback_start_year=int(os.getenv("FLASHBACK_START_YEAR", "1995")),
        flashback_end_year=int(end) if end else None,
    )
