"""Auto-repair actions for Agent 10.

Each `repair_op` value emitted by health_checks.py maps to a function here.
The dispatcher calls the right one with retry + exponential backoff. Repair
state is kept in-memory per-process (sufficient because the scheduler is
single-process).

Repair-op names health_checks.py emits:
  - retry_agent:<slot>   → re-invoke the agent for the affected account
  - cleanup_raw_videos   → delete raw_videos older than N days
  - run_music_scout      → call Agent 9 immediately
  - backoff_anthropic    → wait + retry (no-op here; caller of the API does it)
"""

from __future__ import annotations

import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from core.config_loader import AccountConfig, PipelineConfig
from core.logger import get_logger


# Per-(repair_op, account_handle) attempt counter. Reset when a repair
# succeeds, persisted only in-memory across calls.
_attempts: dict[tuple[str, str], int] = {}


class RepairOutcome:
    """Lightweight result object — keeps dispatcher signature clean."""
    def __init__(self, success: bool, attempts: int, detail: str = "") -> None:
        self.success = success
        self.attempts = attempts
        self.detail = detail


def _key(op: str, account_handle: str | None) -> tuple[str, str]:
    return (op, account_handle or "_global")


def dispatch(
    repair_op: str,
    pipeline: PipelineConfig,
    *,
    account: AccountConfig | None,
    max_retries: int,
    base_backoff_seconds: int,
) -> RepairOutcome:
    """Run the repair with retry+backoff. Returns RepairOutcome.

    Anything that raises during the attempt counts as a failed attempt.
    Successful repair zeroes the attempt counter for next time.
    """
    log = get_logger("health.repair", account.handle if account else None)
    fn = _OPS.get(_op_kind(repair_op))
    if not fn:
        log.error("unknown repair_op", extra={"repair_op": repair_op})
        return RepairOutcome(success=False, attempts=0, detail=f"unknown op: {repair_op}")

    key = _key(repair_op, account.handle if account else None)
    attempts_done = _attempts.get(key, 0)

    for attempt in range(attempts_done, max_retries):
        wait = base_backoff_seconds * (2 ** attempt) if attempt > 0 else 0
        if wait > 0:
            log.info(f"repair backoff {wait}s before attempt {attempt + 1}/{max_retries}",
                     extra={"repair_op": repair_op})
            time.sleep(wait)
        try:
            detail = fn(repair_op, pipeline, account)
            log.info(
                f"repair succeeded on attempt {attempt + 1}",
                extra={"repair_op": repair_op, "detail": detail},
            )
            _attempts.pop(key, None)
            return RepairOutcome(success=True, attempts=attempt + 1, detail=detail)
        except Exception as e:
            log.warning(
                f"repair attempt {attempt + 1}/{max_retries} failed",
                extra={"repair_op": repair_op, "err": str(e)[:300]},
            )
            _attempts[key] = attempt + 1

    return RepairOutcome(
        success=False,
        attempts=_attempts[key],
        detail=f"exhausted {max_retries} attempts",
    )


def _op_kind(repair_op: str) -> str:
    """`retry_agent:scout` → `retry_agent`. Bare ops pass through."""
    return repair_op.split(":", 1)[0]


# ── Individual repair functions ──────────────────────────────────────────

def _retry_agent(
    repair_op: str,
    pipeline: PipelineConfig,
    account: AccountConfig | None,
) -> str:
    """Re-invoke the agent for the affected account. The agent's own
    idempotency (skip-if-output-exists) means a second call is cheap when
    the first half-succeeded.
    """
    if not account:
        raise RuntimeError("retry_agent requires an account context")
    slot = repair_op.split(":", 1)[1] if ":" in repair_op else ""
    if not slot:
        raise RuntimeError(f"retry_agent op missing slot suffix: {repair_op}")

    # Import lazily to avoid cycles (main.py imports this module's parent
    # indirectly through orchestration).
    from main import AGENT_REGISTRY
    entry = AGENT_REGISTRY.get(slot)
    if not entry:
        raise RuntimeError(f"unknown slot in AGENT_REGISTRY: {slot}")
    _agent_name, fn = entry
    fn(account, {"slot": slot, "mode": "auto_repair"})
    return f"retried slot={slot} for @{account.handle}"


def _cleanup_raw_videos(
    _repair_op: str,
    pipeline: PipelineConfig,
    _account: AccountConfig | None,
) -> str:
    """Delete `data/raw_videos/<handle>/<date>/` dirs older than
    `health.auto_repair.raw_video_retention_days` (default 7).
    """
    days = int(
        (pipeline.master.raw.get("health") or {})
        .get("auto_repair", {})
        .get("raw_video_retention_days", 7)
    )
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=days)
    root = Path("data/raw_videos")
    if not root.is_dir():
        return "no raw_videos directory to clean"
    deleted = 0
    bytes_freed = 0
    for handle_dir in root.iterdir():
        if not handle_dir.is_dir():
            continue
        for date_dir in handle_dir.iterdir():
            if not date_dir.is_dir():
                continue
            try:
                # Folder mtime is good enough — videogen writes everything at once.
                mtime = datetime.fromtimestamp(date_dir.stat().st_mtime, tz=timezone.utc)
            except OSError:
                continue
            if mtime >= cutoff:
                continue
            # Sum sizes before deletion for the report.
            for f in date_dir.rglob("*"):
                if f.is_file():
                    try:
                        bytes_freed += f.stat().st_size
                    except OSError:
                        pass
            shutil.rmtree(date_dir, ignore_errors=True)
            deleted += 1
    return f"deleted {deleted} dirs, freed ~{bytes_freed // (1024**2)}MB"


def _run_music_scout(
    _repair_op: str,
    pipeline: PipelineConfig,
    account: AccountConfig | None,
) -> str:
    from agents.music_scout.scout import run as music_scout_run
    if account:
        result = music_scout_run(account, {"slot": "music_scout", "mode": "auto_repair"})
        return f"music_scout ran for @{account.handle}, {len(result.get('tracks', []))} tracks"
    # Fallback: run for all accounts
    counts = []
    for a in pipeline.accounts:
        result = music_scout_run(a, {"slot": "music_scout", "mode": "auto_repair"})
        counts.append(f"@{a.handle}={len(result.get('tracks', []))}")
    return "music_scout ran for all accounts: " + ", ".join(counts)


def _backoff_anthropic(
    _repair_op: str,
    _pipeline: PipelineConfig,
    _account: AccountConfig | None,
) -> str:
    """No-op repair. The Anthropic SDK retries internally; this op exists so
    the monitor can record "yes, we noticed the rate limit." Returning success
    just means "noted." If the rate limit persists across multiple health
    checks, the alerter will fire after `auto_repair.max_retries` failures.
    """
    time.sleep(5)
    return "noted Anthropic rate-limit; deferring to SDK retry"


_OPS: dict[str, Callable[[str, PipelineConfig, AccountConfig | None], str]] = {
    "retry_agent": _retry_agent,
    "cleanup_raw_videos": _cleanup_raw_videos,
    "run_music_scout": _run_music_scout,
    "backoff_anthropic": _backoff_anthropic,
}
