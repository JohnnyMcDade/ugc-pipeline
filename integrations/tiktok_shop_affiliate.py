"""TikTok Shop affiliate stats client.

Pulls click + conversion + revenue for an affiliate account, optionally
broken down by product_id. Used by Agent 8 to compute per-video revenue
attribution and to flag underperforming products.

The TikTok Shop Affiliate Partner Center has an API documented at the
Affiliate-Marketer level; access requires application. The transport here
is a stub for the same reason as the other TikTok integrations — wire it
to whichever surface you have access to.

Returns:
  account_totals(date_range) → {
    "clicks":       int,
    "conversions":  int,
    "revenue_usd":  float,
    "as_of":        str (ISO),
  }

  per_product(date_range) → list[{
    "product_id":   str,
    "clicks":       int,
    "conversions":  int,
    "revenue_usd":  float,
  }]

  per_video(date_range, post_ids) → dict[post_id, {clicks, conversions, revenue_usd}]
"""

from __future__ import annotations

from typing import Any

import requests


class TikTokShopAffiliateError(RuntimeError):
    pass


class TikTokShopAffiliateClient:
    def __init__(self, affiliate_id: str, access_token: str, *, timeout: int = 30) -> None:
        if not affiliate_id:
            raise RuntimeError("affiliate_id empty (TIKTOK_SHOP_AFFILIATE_ID_<HANDLE>)")
        self.affiliate_id = affiliate_id
        self.token = access_token
        self.timeout = timeout
        self.session = requests.Session()
        if access_token:
            self.session.headers["Authorization"] = f"Bearer {access_token}"

    def account_totals(self, *, start_date: str, end_date: str) -> dict[str, Any]:
        return self._fetch("totals", {"start": start_date, "end": end_date})

    def per_product(self, *, start_date: str, end_date: str) -> list[dict[str, Any]]:
        return self._fetch("per_product", {"start": start_date, "end": end_date}) or []

    def per_video(
        self, *, start_date: str, end_date: str, post_ids: list[str]
    ) -> dict[str, dict[str, Any]]:
        return self._fetch("per_video", {
            "start": start_date, "end": end_date, "post_ids": post_ids,
        }) or {}

    def _fetch(self, endpoint: str, params: dict[str, Any]) -> Any:
        raise TikTokShopAffiliateError(
            f"TikTok Shop affiliate {endpoint!r} fetch not yet implemented. "
            "Wire integrations/tiktok_shop_affiliate.py:_fetch to the Partner Center API."
        )
