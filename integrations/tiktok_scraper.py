"""TikTok video scraper used by Agent 2 (hooks).

Like the Creative Center client, this is the seam where the unofficial scraping
layer lives. Wire to one of:
  - TikTokApi (PyPI, unofficial)
  - Apify TikTok scraper
  - Playwright-driven scraper

Each video returned should have at minimum:
  {
    "video_id":              str,
    "url":                   str,
    "author":                str,
    "caption":               str,
    "first_line_transcript": str | None,
    "on_screen_text":        str | None,
    "view_count":            int,
    "like_count":            int,
    "comment_count":         int,
    "share_count":           int,
    "duration_seconds":      float,
    "posted_at":             str (ISO8601),
    "music":                 str | None,
    "hashtags":              list[str],
  }
"""

from __future__ import annotations

import os
from typing import Any


class TikTokScraperError(RuntimeError):
    pass


class TikTokScraperClient:
    def __init__(self) -> None:
        self.session = os.environ.get("TIKTOK_SCRAPER_SESSION")

    def top_videos_by_username(self, username: str, limit: int) -> list[dict[str, Any]]:
        """Return up to `limit` of the user's top videos by view count."""
        return self._fetch(mode="username", query=username.lstrip("@"), limit=limit)

    def top_videos_by_keyword(self, keyword: str, limit: int) -> list[dict[str, Any]]:
        """Return up to `limit` top videos matching `keyword` (hashtag or search)."""
        return self._fetch(mode="keyword", query=keyword, limit=limit)

    def _fetch(self, mode: str, query: str, limit: int) -> list[dict[str, Any]]:
        if not self.session:
            raise TikTokScraperError(
                "TIKTOK_SCRAPER_SESSION not set — wire up the scraper in "
                "integrations/tiktok_scraper.py:_fetch"
            )
        raise NotImplementedError(
            "TikTok scraper not yet implemented. "
            "Plug in TikTokApi / Apify / Playwright here."
        )
