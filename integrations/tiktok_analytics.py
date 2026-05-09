"""TikTok per-video analytics client.

Distinct from `tiktok_publish_client.py` (write side) — this is the read side
for video metrics: views, likes, comments, shares, watch time, completion
rate, profile visits.

The TikTok Business API exposes these via the Research API (research scope)
or Creator Analytics endpoints depending on access tier. Endpoint shape and
field names vary by tier, so the transport is intentionally a stub —
implement against whichever surface your account has.

Returns one dict per video with this shape:
  {
    "post_id":          str,        # TikTok's video id
    "fetched_at":       str (ISO),
    "metrics": {
      "views":          int,
      "likes":          int,
      "comments":       int,
      "shares":         int,
      "profile_visits": int,
      "watch_time_avg_seconds": float,
      "completion_rate":         float,   # 0.0-1.0
    },
  }

Auth: per-account access token (`TIKTOK_SESSION_<HANDLE>` — same env var as
the publish client).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import requests


_DEFAULT_BASE_URL = "https://open.tiktokapis.com"


class TikTokAnalyticsError(RuntimeError):
    pass


class TikTokAnalyticsClient:
    def __init__(self, access_token: str, *, base_url: str = _DEFAULT_BASE_URL, timeout: int = 30) -> None:
        if not access_token:
            raise RuntimeError("TikTok access token empty (TIKTOK_SESSION_*)")
        self.token = access_token
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        })

    def video_metrics(self, post_ids: list[str]) -> list[dict[str, Any]]:
        """Returns one row per post_id. Order matches `post_ids`. Failures
        for individual ids surface as rows with `error` set, not exceptions —
        Agent 8 then continues with whatever it could fetch.
        """
        out: list[dict[str, Any]] = []
        for pid in post_ids:
            try:
                row = self._fetch_one(pid)
                out.append(row)
            except TikTokAnalyticsError as e:
                out.append({
                    "post_id": pid,
                    "fetched_at": datetime.now(tz=timezone.utc).isoformat(),
                    "error": str(e),
                    "metrics": {},
                })
        return out

    def _fetch_one(self, post_id: str) -> dict[str, Any]:
        """Hit TikTok's analytics endpoint for one post.

        Replace this body with the call appropriate to your access tier.
        Common shapes:
          POST /v2/research/video/query/   (Research API)
          GET  /v2/business/post/info/      (Business API)
        Map the fields into the documented return shape.
        """
        raise TikTokAnalyticsError(
            "TikTok analytics fetch not yet implemented. "
            "Wire integrations/tiktok_analytics.py:_fetch_one to your API tier."
        )
