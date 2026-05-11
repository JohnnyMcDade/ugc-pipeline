"""Individual health probe functions.

Each returns a `HealthResult` dict:
  {
    "name":       str,                  # short identifier
    "status":     "ok" | "warn" | "fail",
    "detail":     str,                  # human-readable
    "data":       dict,                 # structured (counts, paths, etc.)
    "repairable": bool,                 # whether auto-repair can attempt fix
    "repair_op":  str | None,           # which repair.py function to call
  }

Health checks are passive — they NEVER call other agents. Repair is its
own module (`repair.py`) so we can audit each repair action separately.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.config_loader import AccountConfig, PipelineConfig
from core.dateutils import pipeline_now, today_str

# (slot, output-path-template, agent function name in AGENT_REGISTRY)
# Templates use {handle} and {date}. Per-day slots are checked once per day.
_PER_ACCOUNT_DAILY_OUTPUTS: dict[str, str] = {
    "scout":        "data/trends/{handle}/{date}/{filename}",   # filename: products.json OR signals.json
    "hooks":        "data/hooks/{handle}/{date}/patterns.json",
    "scriptwriter": "data/scripts/{handle}/{date}/scripts.json",
    "videoprompt":  "data/video_prompts/{handle}/{date}/manifest.json",
    "videogen":     "data/raw_videos/{handle}/{date}/manifest.json",
    "editor":       "data/final_videos/{handle}/{date}/manifest.json",
    "monitor":      "data/analytics/{handle}/{date}/report.json",
}


def _result(name: str, status: str, detail: str = "", **extras: Any) -> dict[str, Any]:
    out = {
        "name": name,
        "status": status,
        "detail": detail,
        "data": extras.pop("data", {}),
        "repairable": extras.pop("repairable", False),
        "repair_op": extras.pop("repair_op", None),
    }
    out.update(extras)
    return out


# ── Agent-freshness checks ───────────────────────────────────────────────

def check_agent_freshness(
    account: AccountConfig,
    *,
    staleness_thresholds: dict[str, int],
) -> list[dict[str, Any]]:
    """For each daily slot, confirm today's output file exists AND is fresh
    enough relative to the slot's scheduled hour. Returns one HealthResult
    per slot.
    """
    results: list[dict[str, Any]] = []
    today = today_str()
    now = pipeline_now()

    for slot, template in _PER_ACCOUNT_DAILY_OUTPUTS.items():
        if slot == "scout":
            # scout writes products.json OR signals.json depending on path
            candidates = [
                Path(template.format(handle=account.handle, date=today, filename="products.json")),
                Path(template.format(handle=account.handle, date=today, filename="signals.json")),
            ]
            path = next((c for c in candidates if c.exists()), candidates[0])
        else:
            path = Path(template.format(handle=account.handle, date=today))

        threshold_min = int(staleness_thresholds.get(slot, 90))

        if not path.exists():
            slot_hour = _expected_hour_for_slot(slot)
            # Only flag as fail if we're past the slot's scheduled hour + threshold.
            if slot_hour is None or now.hour < slot_hour:
                results.append(_result(
                    f"freshness.{slot}.{account.handle}", "ok",
                    detail="not yet scheduled to run today",
                    data={"slot_hour": slot_hour, "now_hour": now.hour},
                ))
                continue
            minutes_past_slot = (now.hour - slot_hour) * 60 + now.minute
            if minutes_past_slot < threshold_min:
                results.append(_result(
                    f"freshness.{slot}.{account.handle}", "warn",
                    detail=f"{slot} hasn't produced today yet (within grace)",
                    data={"minutes_past_slot": minutes_past_slot, "threshold": threshold_min},
                ))
            else:
                results.append(_result(
                    f"freshness.{slot}.{account.handle}", "fail",
                    detail=f"{slot} did not produce {path.name} (stale by {minutes_past_slot - threshold_min}min)",
                    data={"expected_path": str(path),
                          "minutes_past_slot": minutes_past_slot,
                          "threshold": threshold_min},
                    repairable=True,
                    repair_op=f"retry_agent:{slot}",
                ))
        else:
            results.append(_result(
                f"freshness.{slot}.{account.handle}", "ok",
                detail=f"{path.name} present",
                data={"path": str(path), "size_bytes": path.stat().st_size},
            ))
    return results


def _expected_hour_for_slot(slot: str) -> int | None:
    """Approximate scheduled hour. Sourced from master.yaml schedule but
    hard-coded here for the staleness check to avoid a config round-trip.
    """
    return {
        "scout": 6, "hooks": 6, "scriptwriter": 7,
        "videoprompt": 8, "videogen": 8, "editor": 10,
        "publisher_1": 12, "publisher_2": 18, "monitor": 23,
    }.get(slot)


# ── External API health ───────────────────────────────────────────────────

def check_heygen_api() -> dict[str, Any]:
    """Light GET against HeyGen — no credit charge."""
    import requests
    key = os.environ.get("HEYGEN_API_KEY")
    if not key:
        return _result("api.heygen", "fail", "HEYGEN_API_KEY missing",
                       repairable=False)
    try:
        r = requests.get(
            "https://api.heygen.com/v2/voices",
            headers={"X-Api-Key": key, "Accept": "application/json"},
            timeout=10,
        )
        if r.status_code < 400:
            return _result("api.heygen", "ok", f"HTTP {r.status_code}")
        if r.status_code == 401:
            return _result("api.heygen", "fail", "HeyGen rejected the API key (401)",
                           data={"status": r.status_code},
                           repairable=False)
        return _result("api.heygen", "warn", f"HTTP {r.status_code}",
                       data={"status": r.status_code, "body": r.text[:200]})
    except requests.RequestException as e:
        return _result("api.heygen", "fail", f"network error: {e}",
                       repairable=False)


def check_anthropic_api() -> dict[str, Any]:
    """Tiny messages.create with max_tokens=1. ~$0.0001 per check."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return _result("api.anthropic", "fail", "ANTHROPIC_API_KEY missing",
                       repairable=False)
    try:
        from anthropic import Anthropic
        client = Anthropic(api_key=key)
        client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1,
            messages=[{"role": "user", "content": "ping"}],
        )
        return _result("api.anthropic", "ok", "1-token probe succeeded")
    except Exception as e:
        msg = str(e)
        # Rate-limit detection — repairable by backoff
        if "rate_limit" in msg.lower() or "429" in msg:
            return _result("api.anthropic", "warn", f"rate limited: {msg[:150]}",
                           repairable=True, repair_op="backoff_anthropic")
        return _result("api.anthropic", "fail", msg[:200], repairable=False)


def check_tiktok_session(account: AccountConfig) -> dict[str, Any]:
    """Light probe of a TikTok endpoint that requires the per-account
    session. Currently a metadata-only check — we don't want to burn quota.
    Until we have a no-op endpoint to ping, we just check the env var is
    populated and looks like a token (not a literal placeholder).
    """
    creds = account.raw.get("api_credentials") or {}
    token = creds.get("tiktok_session", "")
    if not token:
        return _result(
            f"tiktok_session.{account.handle}", "fail",
            "session token empty — Agent 7 (publisher) will 401",
            repairable=False,
        )
    if token.startswith("PLACEHOLDER") or len(token) < 16:
        return _result(
            f"tiktok_session.{account.handle}", "fail",
            "session token looks like a placeholder",
            data={"token_len": len(token)},
            repairable=False,
        )
    return _result(
        f"tiktok_session.{account.handle}", "ok",
        f"token populated ({len(token)} chars)",
        data={"token_len": len(token)},
    )


# ── Disk health ───────────────────────────────────────────────────────────

def check_disk_space(threshold_gb: int) -> dict[str, Any]:
    """Check free space under data/ — repair will delete old raw_videos."""
    target = Path("data")
    target.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(target)
    free_gb = usage.free / (1024 ** 3)
    if free_gb < threshold_gb:
        return _result(
            "disk.free_space", "fail",
            f"only {free_gb:.1f}GB free, below threshold {threshold_gb}GB",
            data={"free_gb": round(free_gb, 2), "threshold_gb": threshold_gb,
                  "total_gb": round(usage.total / (1024 ** 3), 2)},
            repairable=True,
            repair_op="cleanup_raw_videos",
        )
    return _result(
        "disk.free_space", "ok",
        f"{free_gb:.1f}GB free",
        data={"free_gb": round(free_gb, 2)},
    )


# ── Music catalog health ──────────────────────────────────────────────────

def check_music_catalog(account: AccountConfig) -> dict[str, Any]:
    """A populated catalog manifest is what Agent 7 attaches at upload time.
    Empty manifest → publisher falls back to baked audio (no algorithmic
    boost). Treat as warn, not fail — pipeline still works.
    """
    music_cfg = account.raw.get("music") or {}
    subdir = music_cfg.get("catalog_subdir")
    if not subdir:
        return _result(
            f"music_catalog.{account.handle}", "warn",
            "no music.catalog_subdir in YAML — music_id attribution disabled",
        )
    path = Path("data/music_catalog") / subdir / "manifest.json"
    if not path.exists():
        return _result(
            f"music_catalog.{account.handle}", "warn",
            f"manifest missing at {path}",
            repairable=True,
            repair_op="run_music_scout",
        )
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        return _result(
            f"music_catalog.{account.handle}", "fail",
            f"manifest unreadable: {e}",
            repairable=True,
            repair_op="run_music_scout",
        )
    tracks = doc.get("tracks") or []
    if not tracks:
        return _result(
            f"music_catalog.{account.handle}", "warn",
            "manifest is empty (likely catalog endpoint unwired)",
            data={"warning_from_scout": doc.get("warning")},
            repairable=True,
            repair_op="run_music_scout",
        )
    # Validate each entry has a music_id
    bad = [i for i, t in enumerate(tracks) if not t.get("music_id")]
    if bad:
        return _result(
            f"music_catalog.{account.handle}", "fail",
            f"{len(bad)} of {len(tracks)} tracks missing music_id",
            data={"bad_indices": bad[:5]},
            repairable=True,
            repair_op="run_music_scout",
        )
    return _result(
        f"music_catalog.{account.handle}", "ok",
        f"{len(tracks)} valid tracks",
        data={"track_count": len(tracks)},
    )


# ── Orchestration ─────────────────────────────────────────────────────────

def run_all(pipeline: PipelineConfig) -> list[dict[str, Any]]:
    """Run every health probe and return the flat list of results."""
    results: list[dict[str, Any]] = []
    health_cfg = pipeline.master.raw.get("health") or {}
    staleness = health_cfg.get("agent_staleness_minutes") or {}
    disk_threshold = int((health_cfg.get("auto_repair") or {}).get("disk_low_threshold_gb", 5))

    # Global checks (once)
    results.append(check_heygen_api())
    results.append(check_anthropic_api())
    results.append(check_disk_space(disk_threshold))

    # Per-account checks
    for account in pipeline.accounts:
        results.extend(check_agent_freshness(account, staleness_thresholds=staleness))
        results.append(check_tiktok_session(account))
        results.append(check_music_catalog(account))

    return results
