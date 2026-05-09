"""Underperformer identification.

Three outputs (all written by tracker.py — this module just computes):

  losers:           per-video flags for low ER + sufficient age. Informational.
  killed_patterns:  pattern_ids that performed badly in N consecutive uses.
                    Cumulative. Future Agent 2 reads to deprioritize.
  killed_products:  product_ids that produced no conversions across N videos.
                    Cumulative. Future Agent 1 reads to skip in scout.

"Killing" never deletes a TikTok post — just stops the pipeline from making
more of the same kind of content. Cumulative exclusion lists live at:

  data/analytics/<handle>/exclusions/products.json
  data/analytics/<handle>/exclusions/patterns.json

Today both files are advisory: Agents 1 and 2 don't read them yet (the loop
closes via winners.json, which they DO read). Wiring exclusion-aware scout
and pattern selection is a one-line change in each agent — left as a
follow-up so each prior build stays under its original contract.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any


def identify_losers(
    *,
    per_video: list[dict[str, Any]],
    max_engagement_rate: float,
    min_age_hours: float,
    min_view_floor: int,
) -> list[dict[str, Any]]:
    """A loser is a video that's old enough to judge AND under-engaged.
    The view-floor check distinguishes "actually bad content" from "TikTok
    didn't distribute it" — videos with near-zero views aren't losers, the
    distribution algorithm just didn't pick them up.
    """
    out: list[dict[str, Any]] = []
    for v in per_video:
        m = v.get("metrics") or {}
        age = float(v.get("age_hours", 0.0))
        if age < min_age_hours:
            continue
        if int(m.get("views", 0)) < min_view_floor:
            continue
        er = float(m.get("engagement_rate", 0.0))
        if er <= max_engagement_rate:
            out.append({
                "video_id": v.get("video_id"),
                "tiktok_post_ids": v.get("tiktok_post_ids", []),
                "engagement_rate": er,
                "view_count": int(m.get("views", 0)),
                "age_hours": age,
                "reason": "engagement_below_threshold",
                "source_pattern_id": (v.get("source") or {}).get("source_pattern_id"),
                "source_product_id": (v.get("source") or {}).get("source_product_id"),
            })
    return out


def aggregate_pattern_performance(
    per_video: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Returns {pattern_id: {uses, avg_er, avg_views, video_ids}}."""
    by_pattern: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for v in per_video:
        pid = (v.get("source") or {}).get("source_pattern_id")
        if pid:
            by_pattern[pid].append(v)

    out: dict[str, dict[str, Any]] = {}
    for pid, videos in by_pattern.items():
        ers = [float((v.get("metrics") or {}).get("engagement_rate", 0.0)) for v in videos]
        views = [int((v.get("metrics") or {}).get("views", 0)) for v in videos]
        out[pid] = {
            "uses": len(videos),
            "avg_engagement_rate": round(sum(ers) / len(ers), 5) if ers else 0.0,
            "avg_views": int(sum(views) / len(views)) if views else 0,
            "video_ids": [v.get("video_id") for v in videos],
        }
    return out


def aggregate_product_performance(
    per_video: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Affiliate accounts only. Returns {product_id: {uses, total_conversions, total_revenue, video_ids}}."""
    by_product: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for v in per_video:
        pid = (v.get("source") or {}).get("source_product_id")
        if pid:
            by_product[pid].append(v)

    out: dict[str, dict[str, Any]] = {}
    for pid, videos in by_product.items():
        conversions = sum(int((v.get("shop") or {}).get("conversions", 0)) for v in videos)
        revenue = sum(float((v.get("shop") or {}).get("revenue_usd", 0.0)) for v in videos)
        out[pid] = {
            "uses": len(videos),
            "total_conversions": conversions,
            "total_revenue_usd": round(revenue, 2),
            "video_ids": [v.get("video_id") for v in videos],
        }
    return out


def patterns_to_kill(
    pattern_perf: dict[str, dict[str, Any]],
    *,
    consecutive_uses_threshold: int,
    max_engagement_rate: float,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for pid, agg in pattern_perf.items():
        if agg["uses"] >= consecutive_uses_threshold and agg["avg_engagement_rate"] <= max_engagement_rate:
            out.append({
                "pattern_id": pid,
                "uses": agg["uses"],
                "avg_engagement_rate": agg["avg_engagement_rate"],
                "avg_views": agg["avg_views"],
                "video_ids": agg["video_ids"],
                "reason": (
                    f"avg ER {agg['avg_engagement_rate']:.4f} ≤ {max_engagement_rate} "
                    f"across {agg['uses']} uses"
                ),
            })
    return out


def products_to_kill(
    product_perf: dict[str, dict[str, Any]],
    *,
    consecutive_uses_threshold: int,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for pid, agg in product_perf.items():
        if agg["uses"] >= consecutive_uses_threshold and agg["total_conversions"] == 0:
            out.append({
                "product_id": pid,
                "uses": agg["uses"],
                "total_revenue_usd": agg["total_revenue_usd"],
                "video_ids": agg["video_ids"],
                "reason": f"0 conversions across {agg['uses']} videos",
            })
    return out


def merge_exclusions(
    existing: list[dict[str, Any]],
    new_kills: list[dict[str, Any]],
    *,
    key: str,
) -> list[dict[str, Any]]:
    """Cumulative merge. If a kill exists already, keep the older first_seen
    timestamp but refresh the latest stats.
    """
    by_key = {item[key]: item for item in existing if key in item}
    for k in new_kills:
        kid = k[key]
        if kid in by_key:
            prev = by_key[kid]
            k["first_seen"] = prev.get("first_seen") or prev.get("last_seen")
        by_key[kid] = k
    return list(by_key.values())
