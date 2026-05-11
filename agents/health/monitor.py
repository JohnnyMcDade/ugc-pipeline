"""Agent 10: Self-Repair Monitor (orchestrator).

Every 15 minutes, this function:
  1. Runs every health check in `health_checks.run_all()`.
  2. For each `fail` result with `repairable=True`, dispatches the named
     repair_op via `repair.dispatch()` (with retries + exponential backoff).
  3. Anything that finishes with `success=False` after exhausting attempts
     becomes a Discord alert via `alerter.send_alert()`.
  4. Writes a snapshot to `data/health/<date>/<HHMM>.json` for audit.

Repair-op attempt counters live in-memory (`repair._attempts`), so a check
that fails on one run picks up where it left off on the next.

Daily 7 AM report is in `report.py` — separate cron slot, separate function.

The monitor itself NEVER crashes the scheduler — every exception is caught
and logged. APScheduler's `max_instances=1` on the health_check job
guarantees only one monitor invocation runs at a time.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agents.health import health_checks, repair
from agents.health.alerter import send_alert
from core.config_loader import PipelineConfig
from core.dateutils import pipeline_now, today_str
from core.logger import get_logger

HEALTH_LOG_ROOT = Path("data/health")

# Map fails back to the account they affect (extracted from result.name).
def _account_handle_for(result: dict[str, Any], pipeline: PipelineConfig):
    name = result["name"]
    for a in pipeline.accounts:
        if name.endswith(f".{a.handle}") or f".{a.handle}." in name:
            return a
    return None


def run(pipeline: PipelineConfig, ctx: dict[str, Any]) -> dict[str, Any]:
    log = get_logger("health.monitor")
    health_cfg = pipeline.master.raw.get("health") or {}
    auto_repair_cfg = health_cfg.get("auto_repair") or {}
    max_retries = int(auto_repair_cfg.get("max_retries", 3))
    base_backoff = int(auto_repair_cfg.get("base_backoff_seconds", 30))

    try:
        results = health_checks.run_all(pipeline)
    except Exception as e:
        log.exception("health_checks.run_all crashed; aborting this tick", extra={"err": str(e)})
        return {"error": str(e), "results": []}

    fails = [r for r in results if r["status"] == "fail"]
    warns = [r for r in results if r["status"] == "warn"]
    oks = [r for r in results if r["status"] == "ok"]
    log.info(
        "health snapshot",
        extra={"ok": len(oks), "warn": len(warns), "fail": len(fails)},
    )

    repair_records: list[dict[str, Any]] = []
    alerts_sent: list[dict[str, Any]] = []

    for r in fails:
        if not r.get("repairable"):
            # Non-repairable hard fail → alert immediately
            sent = send_alert(
                title=f"❌ {r['name']}",
                level="red",
                description=r["detail"],
                fields=[{"name": "repair", "value": "not auto-repairable — human attention needed"}],
                footer=f"snapshot {pipeline_now().strftime('%Y-%m-%d %H:%M')}",
            )
            alerts_sent.append({"name": r["name"], "discord_sent": sent})
            continue

        affected_account = _account_handle_for(r, pipeline)
        outcome = repair.dispatch(
            r["repair_op"], pipeline,
            account=affected_account,
            max_retries=max_retries,
            base_backoff_seconds=base_backoff,
        )
        repair_records.append({
            "name": r["name"],
            "repair_op": r["repair_op"],
            "success": outcome.success,
            "attempts": outcome.attempts,
            "detail": outcome.detail,
        })
        if not outcome.success:
            # Alert only after we've exhausted attempts
            sent = send_alert(
                title=f"❌ Auto-repair failed: {r['name']}",
                level="red",
                description=r["detail"],
                fields=[
                    {"name": "repair_op", "value": r["repair_op"]},
                    {"name": "attempts", "value": f"{outcome.attempts}/{max_retries}"},
                    {"name": "outcome", "value": outcome.detail[:1000] or "(no detail)"},
                ],
                footer=f"snapshot {pipeline_now().strftime('%Y-%m-%d %H:%M')}",
            )
            alerts_sent.append({"name": r["name"], "discord_sent": sent})

    # Persist a snapshot for audit. Keyed by HHMM so 15-minute cadence
    # gives ~96 files per day, manageable.
    snap_path = HEALTH_LOG_ROOT / today_str() / f"{pipeline_now().strftime('%H%M')}.json"
    snap_path.parent.mkdir(parents=True, exist_ok=True)
    snap_path.write_text(json.dumps({
        "ts": datetime.now(tz=timezone.utc).isoformat(),
        "results": results,
        "repairs": repair_records,
        "alerts": alerts_sent,
        "summary": {"ok": len(oks), "warn": len(warns), "fail": len(fails)},
    }, indent=2, default=str), encoding="utf-8")

    return {
        "snapshot_path": str(snap_path),
        "summary": {"ok": len(oks), "warn": len(warns), "fail": len(fails),
                    "repairs_run": len(repair_records),
                    "alerts_sent": len(alerts_sent)},
    }
