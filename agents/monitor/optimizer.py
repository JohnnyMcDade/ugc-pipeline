"""Winner identification + winners.json writing.

Reads the per-video analytics document tracker.py builds and writes:

  data/analytics/<handle>/<date>/winners.json

Schema is the one Agent 2 (hooks/analyzer.py) and Agent 7 (publisher/hashtag_gen.py)
already consume:

  {
    "winners": [
      {
        "video_id": "...",
        "hook": "...",
        "hook_type": "POV",
        "source_pattern_id": "...",
        "engagement_rate": 0.087,
        "view_count": 152000,
        "hashtags": ["..."]
      }
    ]
  }
"""

from __future__ import annotations

from typing import Any


def identify_winners(
    *,
    per_video: list[dict[str, Any]],
    min_engagement_rate: float,
    min_view_count: int,
    take_top_n: int,
) -> list[dict[str, Any]]:
    """`per_video` items must have:
      - source.hook, source.hook_type, source.source_pattern_id, source.hashtags
      - metrics.views, metrics.engagement_rate
    Filters to those above threshold, sorts by engagement_rate desc, caps.
    """
    candidates: list[tuple[float, dict[str, Any]]] = []
    for v in per_video:
        m = v.get("metrics") or {}
        er = float(m.get("engagement_rate", 0.0))
        views = int(m.get("views", 0))
        if er < min_engagement_rate:
            continue
        if views < min_view_count:
            continue
        src = v.get("source") or {}
        if not src.get("hook"):
            continue
        candidates.append((er, {
            "video_id": v.get("video_id"),
            "tiktok_post_ids": v.get("tiktok_post_ids", []),
            "hook": src.get("hook"),
            "hook_type": src.get("hook_type", "other"),
            "source_pattern_id": src.get("source_pattern_id"),
            "engagement_rate": er,
            "view_count": views,
            "hashtags": src.get("hashtags", []),
        }))

    candidates.sort(key=lambda t: t[0], reverse=True)
    return [row for _, row in candidates[:take_top_n]]
