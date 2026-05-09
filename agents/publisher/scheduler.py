"""Per-slot publish decisions.

The master cron (core/scheduler.py) fires this agent at the cron times defined
in master.yaml — `publisher_1` (12:00) and `publisher_2` (18:00) by default.
This module decides, *given* a slot has fired:

  - Which video to publish from today's editor manifest.
  - Whether the account has already hit its `post_frequency` cap for the day.

Picks are deterministic so re-runs at the same slot pick the same video. We
never publish the same `video_id` twice — the published_log is the source of
truth for what's already shipped.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from core.config_loader import AccountConfig
from core.dateutils import today_str

PUBLISHED_LOG_ROOT = Path("data/published_log")
FINAL_VIDEOS_ROOT = Path("data/final_videos")


def already_published_video_ids(handle: str) -> set[str]:
    """Returns the set of video_ids already published TODAY for this account."""
    day_dir = PUBLISHED_LOG_ROOT / handle / today_str()
    if not day_dir.is_dir():
        return set()
    out: set[str] = set()
    for p in day_dir.glob("*.json"):
        if p.name == "manifest.json":
            continue
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        vid = doc.get("video_id")
        # Only count actually-published entries — failed attempts don't burn the slot.
        if vid and (doc.get("publish_status") in {"PUBLISH_COMPLETE", "PUBLISHED"}):
            out.add(vid)
    return out


def candidate_videos(handle: str) -> list[dict[str, Any]]:
    """Returns the editor's manifest items for today, in manifest order. Items
    have at least `video_id`, `platform`, `final_path`.
    """
    manifest_path = FINAL_VIDEOS_ROOT / handle / today_str() / "manifest.json"
    if not manifest_path.exists():
        return []
    try:
        doc = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return list(doc.get("items", []))


def pick_video(account: AccountConfig, slot: str) -> dict[str, Any] | None:
    """Returns the next video to publish for `account` at this slot, or None
    if the account has hit its post_frequency cap or has nothing left.
    """
    posted = already_published_video_ids(account.handle)
    if len(posted) >= int(account.post_frequency):
        return None

    for item in candidate_videos(account.handle):
        if item.get("video_id") in posted:
            continue
        if item.get("error"):
            continue
        return item
    return None


def slot_for_now(timezone_name: str, slots: dict[str, str]) -> str | None:
    """Best-fit slot lookup. Useful when running publisher one-shot ad-hoc and
    we want it to behave like whichever scheduled slot is closest to now.

    Returns the slot whose cron-hour equals the current hour, or None.
    """
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo(timezone_name))
    except Exception:
        now = datetime.now()
    hour = now.hour
    for slot, cron in slots.items():
        # cron format: "<min> <hour> ..." — pull the hour field.
        parts = cron.split()
        if len(parts) >= 2 and parts[1].isdigit() and int(parts[1]) == hour:
            return slot
    return None
