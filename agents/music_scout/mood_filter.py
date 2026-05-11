"""Per-account mood filtering + scoring for the music scout.

The catalog client returns tracks tagged with HeyGen-side moods like
"upbeat", "lofi", "energetic", "chill", etc. This module picks the ones that
match an account's persona (moods listed in `account.raw["music"]["moods"]`)
and ranks them by trending_score with a small boost for full-mood matches.
"""

from __future__ import annotations

from typing import Any


def score_track_for_account(track: dict[str, Any], wanted_moods: list[str]) -> float:
    """Combined score: trending_score + small bonus per matching mood.

    Pure trending (no mood match) caps at the raw `trending_score`. Full
    mood overlap adds up to +0.10. We never re-rank by raw mood match alone
    — TikTok's trending score is the dominant signal because it's what
    drives algorithmic reach.
    """
    base = float(track.get("trending_score") or 0.0)
    track_moods = {m.lower() for m in (track.get("moods") or [])}
    wanted = {m.lower() for m in wanted_moods}
    if not wanted:
        return base
    overlap = len(track_moods & wanted) / max(1, len(wanted))
    return round(min(1.0, base + 0.10 * overlap), 4)


def select_top_tracks(
    tracks: list[dict[str, Any]],
    *,
    wanted_moods: list[str],
    top_n: int,
) -> list[dict[str, Any]]:
    """Returns tracks augmented with `final_score`, sorted desc, capped."""
    out: list[dict[str, Any]] = []
    for t in tracks:
        score = score_track_for_account(t, wanted_moods)
        out.append({**t, "final_score": score})
    out.sort(key=lambda t: t["final_score"], reverse=True)
    return out[:top_n]
