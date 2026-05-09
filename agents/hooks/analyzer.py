"""Agent 2: Hook researcher.

For each account:
  1. Pull top videos from every reference_account in the YAML.
  2. Pull top videos for niche keywords (broader signal, optional).
  3. Filter for minimum engagement, dedupe.
  4. Load yesterday's winners from Agent 8 output (if it exists).
  5. identify_hooks() → cluster_patterns() via hook_extractor.
  6. Write data/hooks/<handle>/<YYYY-MM-DD>/patterns.json.

This output is what Agent 3 (scriptwriter) reads tomorrow morning.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agents.hooks.hook_extractor import cluster_patterns, identify_hooks
from core.config_loader import AccountConfig, load_master
from core.dateutils import today_str, yesterday_str
from core.logger import get_logger
from integrations.claude_api import ClaudeClient
from integrations.tiktok_scraper import TikTokScraperClient

OUTPUT_ROOT = Path("data/hooks")
ANALYTICS_ROOT = Path("data/analytics")

# Floor for what counts as "top" — drops videos that didn't get traction.
_MIN_VIEW_COUNT = 50_000
_MIN_ENGAGEMENT_RATE = 0.02


def _today_dir(handle: str) -> Path:
    d = OUTPUT_ROOT / handle / today_str()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def _engagement(v: dict[str, Any]) -> float:
    views = max(1, int(v.get("view_count", 0)))
    return (
        int(v.get("like_count", 0))
        + int(v.get("comment_count", 0))
        + int(v.get("share_count", 0))
    ) / views


def _yesterday_winners(handle: str) -> list[dict[str, Any]] | None:
    """Read Agent 8's output for yesterday, if present. Schema is whatever
    Agent 8 writes; we pass it through to Claude as-is.
    """
    path = ANALYTICS_ROOT / handle / yesterday_str() / "winners.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("winners", [])
    except (json.JSONDecodeError, OSError):
        return None


def run(account: AccountConfig, ctx: dict[str, Any]) -> dict[str, Any]:
    log = get_logger("hooks", account.handle)
    out_dir = _today_dir(account.handle)

    hooks_cfg = account.hooks
    top_n = int(hooks_cfg.get("top_n_to_analyze", 30))
    reference_accounts: list[str] = hooks_cfg.get("reference_accounts", [])

    scraper = TikTokScraperClient()

    # 1+2. Collect candidate videos.
    candidates: list[dict[str, Any]] = []
    for ref in reference_accounts:
        try:
            candidates.extend(scraper.top_videos_by_username(ref, limit=top_n))
        except Exception as e:
            log.warning("scraper failed for username", extra={"username": ref, "err": str(e)})

    # Niche keywords broaden the pool — for the affiliate accounts those
    # come from scout config; for passivepoly we synthesize from the niche.
    niche_keywords = account.scout.get("keywords") or [account.niche.replace("_", " ")]
    for kw in niche_keywords[:3]:  # cap at 3 keywords to bound scraper cost
        try:
            candidates.extend(scraper.top_videos_by_keyword(kw, limit=top_n))
        except Exception as e:
            log.warning("scraper failed for keyword", extra={"keyword": kw, "err": str(e)})

    log.info("scraper raw count", extra={"raw": len(candidates)})

    # 3. Filter + dedupe.
    seen: set[str] = set()
    filtered: list[dict[str, Any]] = []
    for v in candidates:
        vid = v.get("video_id")
        if not vid or vid in seen:
            continue
        if int(v.get("view_count", 0)) < _MIN_VIEW_COUNT:
            continue
        if _engagement(v) < _MIN_ENGAGEMENT_RATE:
            continue
        seen.add(vid)
        filtered.append(v)
    filtered.sort(key=_engagement, reverse=True)
    filtered = filtered[: top_n * 2]  # cap so identify_hooks stays bounded

    if not filtered:
        log.warning("no videos after filter — emitting empty patterns file")
        result = {
            "account": account.handle,
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
            "source_videos_analyzed": 0,
            "yesterday_winners_input": False,
            "patterns": [],
        }
        _write_json(out_dir / "patterns.json", result)
        return result

    # 4. Yesterday's winners (optional).
    winners = _yesterday_winners(account.handle)

    # 5. Two-stage extraction.
    claude = ClaudeClient()
    model = load_master(Path("config/master.yaml")).models["claude"]["fast"]

    identified = identify_hooks(account, filtered, claude, model)
    patterns = cluster_patterns(account, identified, winners, claude, model)

    # 6. Persist.
    result = {
        "account": account.handle,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "source_videos_analyzed": len(filtered),
        "hooks_identified": len(identified),
        "yesterday_winners_input": winners is not None,
        "patterns": patterns,
    }
    out_path = out_dir / "patterns.json"
    _write_json(out_path, result)
    log.info("hooks complete", extra={"out": str(out_path), "patterns": len(patterns)})
    return result
