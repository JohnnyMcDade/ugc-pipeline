"""TikTok Creative Center client.

Creative Center has no official public API for trending products; production
uses an authenticated session + the internal endpoints the web UI calls. This
module isolates that detail behind a clean interface so the scout doesn't have
to care.

Each `search_products` call returns a list of dicts with this shape:
  {
    "product_id":      str,   # CC's stable id
    "title":           str,
    "category":        str,
    "price_usd":       float,
    "commission_pct":  float,  # 0.0-1.0
    "post_count_7d":   int,
    "post_count_prev_7d": int, # for velocity calculation downstream
    "rating":          float,
    "url":             str,
  }

NOTE: the network layer below is intentionally a stub that raises
NotImplementedError. Wire it to either:
  (a) an authenticated CC session via `tiktok-creative-center-api` PyPI lib, or
  (b) a Playwright scraper.
The scout will work end-to-end the moment `_fetch_raw` returns data.
"""

from __future__ import annotations

import os
from typing import Any


class CreativeCenterError(RuntimeError):
    pass


class CreativeCenterClient:
    def __init__(self, region: str, period_days: int, min_post_count: int) -> None:
        self.region = region
        self.period_days = period_days
        self.min_post_count = min_post_count
        self.session_cookie = os.environ.get("TIKTOK_CC_SESSION")

    def search_products(
        self,
        keywords: list[str],
        exclude_keywords: list[str] | None = None,
        price_range: list[float] | None = None,
    ) -> list[dict[str, Any]]:
        exclude = set((exclude_keywords or []))
        results: list[dict[str, Any]] = []
        seen_ids: set[str] = set()

        for kw in keywords:
            page = self._fetch_raw(keyword=kw)
            for item in page:
                pid = item.get("product_id")
                if not pid or pid in seen_ids:
                    continue
                if any(bad.lower() in item.get("title", "").lower() for bad in exclude):
                    continue
                if price_range:
                    lo, hi = price_range
                    price = item.get("price_usd", 0.0)
                    if price < lo or price > hi:
                        continue
                if item.get("post_count_7d", 0) < self.min_post_count:
                    continue
                seen_ids.add(pid)
                results.append(item)
        return results

    def _fetch_raw(self, keyword: str) -> list[dict[str, Any]]:
        """Hit Creative Center for `keyword`. Raise until wired up.

        Replace this body with either the unofficial-API call or the scraper.
        Honor `self.region`, `self.period_days`, `self.session_cookie`.
        """
        if not self.session_cookie:
            raise CreativeCenterError(
                "TIKTOK_CC_SESSION not set — wire up Creative Center auth in "
                "integrations/tiktok_creative_center.py:_fetch_raw"
            )
        raise NotImplementedError(
            "Creative Center fetch not yet implemented. "
            "Plug in tiktok-creative-center-api or a Playwright scraper here."
        )
