"""Agent 9: Music catalog scout.

Weekly cadence (Sunday 5 AM ET). Pulls TikTok's Commercial Music Library
catalog filtered by each account's mood profile, ranks by trending_score
plus a small mood-fit bonus, and writes a manifest each account can read.

NOT a downloader — this never fetches MP3s. Music attribution happens at
TikTok upload time via the Content Posting API's `music_id` field. See
[CLAUDE.md] for why this is the right architecture (algorithmic boost +
ToS-compliant + no ffmpeg complexity).

Outputs (per account):
  data/music_catalog/<catalog_subdir>/manifest.json

Append-only usage log (shared across accounts):
  data/music_catalog/music_log.json
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agents.music_scout.mood_filter import select_top_tracks
from core.config_loader import AccountConfig, load_master
from core.dateutils import today_str
from core.logger import get_logger
from integrations.tiktok_music_catalog import (
    TikTokMusicCatalogClient,
    TikTokMusicCatalogError,
)

MUSIC_CATALOG_ROOT = Path("data/music_catalog")
MUSIC_LOG_PATH = MUSIC_CATALOG_ROOT / "music_log.json"


def _account_music_cfg(account: AccountConfig) -> dict[str, Any]:
    cfg = account.raw.get("music") or {}
    if not cfg.get("moods"):
        raise RuntimeError(
            f"@{account.handle}: missing music.moods in account YAML — "
            "Agent 9 needs at least one mood to filter the catalog by."
        )
    if not cfg.get("catalog_subdir"):
        raise RuntimeError(
            f"@{account.handle}: missing music.catalog_subdir in account YAML"
        )
    return cfg


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def run(account: AccountConfig, ctx: dict[str, Any]) -> dict[str, Any]:
    """Single-account entry point. Called weekly by the scheduler.

    Returns the manifest dict (also written to disk).
    """
    log = get_logger("music_scout", account.handle)
    music_cfg = _account_music_cfg(account)
    moods: list[str] = music_cfg["moods"]
    genres: list[str] = music_cfg.get("genres") or []
    top_n = int(music_cfg.get("top_n_to_keep", 20))

    log.info(
        "music scout starting",
        extra={"moods": moods, "genres": genres, "top_n": top_n},
    )

    out_path = MUSIC_CATALOG_ROOT / music_cfg["catalog_subdir"] / "manifest.json"

    try:
        client = TikTokMusicCatalogClient()
        raw_tracks = client.list_commercial_music(
            moods=moods, genres=genres, limit=top_n * 3, sort="trending",
        )
    except (TikTokMusicCatalogError, NotImplementedError) as e:
        # Endpoint not yet wired — write a placeholder manifest so downstream
        # agents see "music scout ran but found nothing" instead of "music
        # scout never ran". This distinction matters for the health monitor.
        log.warning("music catalog fetch unavailable, writing empty manifest",
                    extra={"err": str(e)[:200]})
        manifest = {
            "account": account.handle,
            "catalog_subdir": music_cfg["catalog_subdir"],
            "fetched_at": datetime.now(tz=timezone.utc).isoformat(),
            "date": today_str(),
            "moods_filter": moods,
            "genres_filter": genres,
            "tracks": [],
            "warning": "catalog_endpoint_unwired",
            "endpoint_error": str(e)[:300],
        }
        _write_json(out_path, manifest)
        return manifest

    ranked = select_top_tracks(raw_tracks, wanted_moods=moods, top_n=top_n)
    manifest = {
        "account": account.handle,
        "catalog_subdir": music_cfg["catalog_subdir"],
        "fetched_at": datetime.now(tz=timezone.utc).isoformat(),
        "date": today_str(),
        "moods_filter": moods,
        "genres_filter": genres,
        "tracks": ranked,
    }
    _write_json(out_path, manifest)
    log.info(
        "music scout complete",
        extra={"out": str(out_path), "tracks": len(ranked),
               "top_score": ranked[0]["final_score"] if ranked else None},
    )
    return manifest


# --- public utilities used by the publisher to pick the music_id to attach
# at upload time. Kept here (next to the catalog producer) so the schema
# contract is co-located.

def pick_music_id_for_video(
    account: AccountConfig,
    *,
    recent_window_days: int = 7,
) -> dict[str, Any] | None:
    """Returns the next music track to use for an account, or None.

    Strategy: top of the manifest, excluding any music_id this account used
    in the last `recent_window_days`. Falls back to top-of-manifest if every
    candidate was recently used (better to repeat than to skip music).
    """
    music_cfg = (account.raw.get("music") or {})
    sub = music_cfg.get("catalog_subdir")
    if not sub:
        return None
    manifest = _read_json(MUSIC_CATALOG_ROOT / sub / "manifest.json")
    if not manifest:
        return None
    tracks = manifest.get("tracks") or []
    if not tracks:
        return None

    recent_ids = _recently_used_ids(account.handle, days=recent_window_days)
    for t in tracks:
        if t.get("music_id") not in recent_ids:
            return t
    return tracks[0]


def log_music_use(
    *, account_handle: str, video_id: str, track: dict[str, Any],
) -> None:
    """Append an entry to data/music_catalog/music_log.json.

    Idempotent on `(video_id, music_id)`: if you call this twice for the
    same pair (e.g. a publisher retry), only one entry is kept.
    """
    log_doc = _read_json(MUSIC_LOG_PATH) or {"entries": []}
    entries = list(log_doc.get("entries", []))
    key = (video_id, track.get("music_id"))
    entries = [e for e in entries
               if (e.get("video_id"), e.get("music_id")) != key]
    entries.append({
        "used_at": datetime.now(tz=timezone.utc).isoformat(),
        "video_id": video_id,
        "account": account_handle,
        "music_id": track.get("music_id"),
        "title": track.get("title"),
        "artist": track.get("artist"),
        "trending_score_at_use": track.get("trending_score"),
        "final_score_at_use": track.get("final_score"),
    })
    _write_json(MUSIC_LOG_PATH, {"entries": entries})


def _recently_used_ids(handle: str, *, days: int) -> set[str]:
    log_doc = _read_json(MUSIC_LOG_PATH) or {"entries": []}
    cutoff = datetime.now(tz=timezone.utc).timestamp() - days * 86400
    out: set[str] = set()
    for e in log_doc.get("entries", []):
        if e.get("account") != handle:
            continue
        try:
            ts = datetime.fromisoformat(e["used_at"].replace("Z", "+00:00")).timestamp()
        except (KeyError, ValueError):
            continue
        if ts >= cutoff and e.get("music_id"):
            out.add(e["music_id"])
    return out
