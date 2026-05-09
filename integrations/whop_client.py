"""Whop API client (passivepoly subscription stats).

Real implementation against Whop's documented v5 API. Used by Agent 8 to
compute the @passivepoly daily subscription numbers (paid signups, MRR
added, active subscribers) and per-day revenue.

Endpoints:
  GET /v5/memberships         — list memberships (with `created_at` filter)
  GET /v5/payments            — list payments (with `created_at` filter)

Auth: WHOP_API_KEY env, Bearer token.

This client is read-only — never modifies Whop state.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

import requests


_DEFAULT_BASE_URL = "https://api.whop.com"


class WhopAPIError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"Whop API {status}: {message}")
        self.status = status


class WhopClient:
    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str = _DEFAULT_BASE_URL,
        timeout: int = 30,
    ) -> None:
        key = api_key or os.environ.get("WHOP_API_KEY")
        if not key:
            raise RuntimeError("WHOP_API_KEY not set")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
        })

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}{path}"
        r = self.session.get(url, params=params, timeout=self.timeout)
        if r.status_code >= 400:
            try:
                detail = r.json()
            except ValueError:
                detail = r.text[:300]
            raise WhopAPIError(r.status_code, str(detail))
        return r.json()

    def _paginate(self, path: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        params = dict(params or {})
        params.setdefault("per", 50)
        page = 1
        out: list[dict[str, Any]] = []
        while True:
            params["page"] = page
            doc = self._get(path, params)
            items = doc.get("data") if isinstance(doc, dict) else doc
            if not items:
                break
            out.extend(items)
            pagination = (doc.get("pagination") or {}) if isinstance(doc, dict) else {}
            total_pages = int(pagination.get("total_pages", page))
            if page >= total_pages:
                break
            page += 1
        return out

    def memberships_created_between(
        self, *, start_iso: str, end_iso: str
    ) -> list[dict[str, Any]]:
        return self._paginate("/v5/memberships", {
            "filter[created_at_gte]": start_iso,
            "filter[created_at_lte]": end_iso,
        })

    def payments_created_between(
        self, *, start_iso: str, end_iso: str
    ) -> list[dict[str, Any]]:
        return self._paginate("/v5/payments", {
            "filter[created_at_gte]": start_iso,
            "filter[created_at_lte]": end_iso,
        })

    def stats_for_window(self, *, start_iso: str, end_iso: str) -> dict[str, Any]:
        """Aggregates memberships + payments for a window. Returns counts +
        revenue. All values are best-effort and tolerate missing fields.
        """
        memberships = self.memberships_created_between(start_iso=start_iso, end_iso=end_iso)
        payments = self.payments_created_between(start_iso=start_iso, end_iso=end_iso)

        trial_signups = sum(1 for m in memberships if (m.get("status") or "").lower() in {"trialing", "trial"})
        paid_signups = sum(1 for m in memberships if (m.get("status") or "").lower() in {"active", "completed", "paid"})

        revenue_cents = 0
        for p in payments:
            if (p.get("status") or "").lower() in {"completed", "succeeded", "paid"}:
                revenue_cents += int(p.get("amount") or p.get("final_amount") or 0)

        return {
            "as_of": datetime.now(tz=timezone.utc).isoformat(),
            "window_start": start_iso,
            "window_end": end_iso,
            "trial_signups": trial_signups,
            "paid_signups": paid_signups,
            "memberships_total": len(memberships),
            "revenue_usd": round(revenue_cents / 100.0, 2),
            "payments_count": len(payments),
        }
