"""Read-only client for the live PassivePoly backend (JohnnyMcDade/polymarket-bot
running on Railway). Pulls the data the scout uses to assemble today's
@passivepoly content slate.

This module NEVER writes to the backend. If the backend doesn't expose the
needed endpoints yet, add them to `launcher.py` on the backend side — do not
work around it with database scraping or log parsing.

Expected endpoints (add to launcher.py if missing):
  GET /api/alerts/today                 → list of alert events (whale tracker)
  GET /api/stats/win-loss?days=N        → {wins, losses, pnl_pct, ...}
  GET /api/whales/biggest?hours=N       → single biggest move in window
  GET /api/markets/notable-resolution   → most recent notable market resolution

Auth: Bearer token in PASSIVEPOLY_BACKEND_TOKEN.
"""

from __future__ import annotations

from typing import Any

import requests


class PassivePolyBackend:
    def __init__(self, base_url: str, token: str, timeout: int = 15) -> None:
        if not base_url:
            raise RuntimeError("PassivePoly backend URL not configured")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        r = requests.get(
            f"{self.base_url}{path}",
            params=params,
            headers=headers,
            timeout=self.timeout,
        )
        r.raise_for_status()
        return r.json()

    def daily_alerts(self) -> list[dict[str, Any]]:
        return self._get("/api/alerts/today")

    def win_loss(self, days: int = 7) -> dict[str, Any]:
        return self._get("/api/stats/win-loss", params={"days": days})

    def biggest_whale_move(self, hours: int = 24) -> dict[str, Any]:
        return self._get("/api/whales/biggest", params={"hours": hours})

    def notable_resolution(self) -> dict[str, Any]:
        return self._get("/api/markets/notable-resolution")
