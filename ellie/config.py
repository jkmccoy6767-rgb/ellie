"""Runtime settings, read from environment variables with safe defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUNDLED_CONSTITUENTS = ROOT / "data" / "sp500_constituents.csv"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    db_path: Path = field(
        default_factory=lambda: Path(os.environ.get("ELLIE_DB_PATH", ROOT / "data" / "ellie.db"))
    )
    # "auto" tries live sources and falls back to synthetic demo data if none respond.
    data_mode: str = field(default_factory=lambda: os.environ.get("ELLIE_DATA_MODE", "auto"))
    history_days: int = field(default_factory=lambda: _env_int("ELLIE_HISTORY_DAYS", 3 * 365))
    # SEC EDGAR requires a descriptive User-Agent with a contact address.
    sec_user_agent: str = field(
        default_factory=lambda: os.environ.get(
            "ELLIE_SEC_USER_AGENT", "Ellie Research Platform admin@example.com"
        )
    )
    fred_api_key: str | None = field(default_factory=lambda: os.environ.get("ELLIE_FRED_API_KEY"))
    max_workers: int = field(default_factory=lambda: _env_int("ELLIE_MAX_WORKERS", 8))
    http_timeout: int = field(default_factory=lambda: _env_int("ELLIE_HTTP_TIMEOUT", 20))
    # Relative close-price difference between sources that raises a reconciliation issue.
    reconcile_tolerance: float = field(
        default_factory=lambda: float(os.environ.get("ELLIE_RECONCILE_TOLERANCE", 0.005))
    )


def get_settings() -> Settings:
    return Settings()
