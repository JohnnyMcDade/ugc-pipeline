"""Pipeline-timezone-aware date helpers.

Every agent reads/writes paths like `data/<stage>/<handle>/<YYYY-MM-DD>/`. The
date string MUST come from the timezone configured in master.yaml, not UTC,
or you get a real bug: Agent 8 fires at 23:00 America/New_York which is
04:00 UTC the *next* day — using UTC, monitor would write to tomorrow's
folder and read from yesterday's published_log, finding nothing.

Use `today_str()` / `yesterday_str()` everywhere instead of formatting UTC
directly. The timezone is read from master.yaml once and cached process-wide.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo


@lru_cache(maxsize=1)
def _tz() -> ZoneInfo:
    # Lazy import to avoid a circular dependency at module-load time
    # (config_loader imports nothing from core, but better safe).
    from core.config_loader import load_master
    return ZoneInfo(load_master(Path("config/master.yaml")).timezone)


def pipeline_now() -> datetime:
    """Wall-clock 'now' in the pipeline's configured timezone."""
    return datetime.now(_tz())


def today_str() -> str:
    return pipeline_now().strftime("%Y-%m-%d")


def yesterday_str() -> str:
    return (pipeline_now() - timedelta(days=1)).strftime("%Y-%m-%d")


def days_ago_str(n: int) -> str:
    return (pipeline_now() - timedelta(days=int(n))).strftime("%Y-%m-%d")
