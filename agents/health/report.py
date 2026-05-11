"""Daily 7 AM health report — Discord embed.

Pulls yesterday's analytics from each account, formats a single Discord
embed with per-account status, totals, open issues, and external-service
notes (HeyGen credits, Anthropic usage — currently stubbed).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from agents.health.alerter import send_report
from agents.health.health_checks import run_all
from core.config_loader import PipelineConfig
from core.dateutils import pipeline_now, yesterday_str
from core.logger import get_logger


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _account_status(
    account_handle: str,
    yesterday: str,
    health_results: list[dict[str, Any]],
) -> tuple[str, str]:
    """Returns (light, summary_line)."""
    own = [r for r in health_results if r["name"].endswith(f".{account_handle}")
           or f".{account_handle}." in r["name"]]
    fails = [r for r in own if r["status"] == "fail"]
    warns = [r for r in own if r["status"] == "warn"]

    pub_log = _read_json(Path("data/published_log") / account_handle / yesterday / "manifest.json")
    posts = len((pub_log or {}).get("items", []))
    succeeded_posts = len([i for i in (pub_log or {}).get("items", [])
                           if i.get("publish_status") in ("PUBLISH_COMPLETE", "PUBLISHED")])

    if fails:
        light = "🔴"
    elif warns:
        light = "🟡"
    else:
        light = "🟢"
    return light, f"{light} **@{account_handle}** — {succeeded_posts}/{posts} posts shipped, {len(warns)}w / {len(fails)}f"


def _yesterday_totals(pipeline: PipelineConfig, yesterday: str) -> dict[str, Any]:
    totals = {"posts": 0, "shop_revenue_usd": 0.0, "whop_revenue_usd": 0.0,
              "views": 0, "errors": 0}
    for a in pipeline.accounts:
        pub = _read_json(Path("data/published_log") / a.handle / yesterday / "manifest.json")
        if pub:
            totals["posts"] += len([
                i for i in pub.get("items", [])
                if i.get("publish_status") in ("PUBLISH_COMPLETE", "PUBLISHED")
            ])
        rep = _read_json(Path("data/analytics") / a.handle / yesterday / "report.json")
        if rep:
            at = rep.get("account_totals") or {}
            totals["views"] += int(at.get("views", 0))
            st = rep.get("shop_totals") or {}
            totals["shop_revenue_usd"] += float(st.get("revenue_usd", 0))
            ws = rep.get("whop_stats") or {}
            totals["whop_revenue_usd"] += float(ws.get("revenue_usd", 0))
    return totals


def _external_quota_stubs() -> str:
    """HeyGen and Anthropic don't expose remaining-quota via API in a
    stable form. Stub for now, called out so the report doesn't look broken.
    """
    return ("• HeyGen credits remaining: _(stubbed — check dashboard at app.heygen.com)_\n"
            "• Anthropic usage: _(stubbed — check console.anthropic.com)_")


def _open_issues(health_results: list[dict[str, Any]]) -> str:
    """Currently-failing checks that auto-repair didn't fix. Surfaces what
    needs human attention.
    """
    fails = [r for r in health_results if r["status"] == "fail"]
    if not fails:
        return "_none_"
    return "\n".join(f"• `{r['name']}` — {r['detail']}" for r in fails[:10])


def run(pipeline: PipelineConfig, ctx: dict[str, Any]) -> dict[str, Any]:
    log = get_logger("health.report")
    log.info("daily health report generating")

    health_results = run_all(pipeline)
    yesterday = yesterday_str()
    totals = _yesterday_totals(pipeline, yesterday)

    # Account status lights
    account_lines = [_account_status(a.handle, yesterday, health_results)[1]
                     for a in pipeline.accounts]

    sections: list[tuple[str, str]] = [
        ("Account status", "\n".join(account_lines) or "_no accounts_"),
        (
            "Yesterday's totals",
            f"• Posts shipped: **{totals['posts']}**\n"
            f"• Views: {totals['views']:,}\n"
            f"• Shop revenue: ${totals['shop_revenue_usd']:.2f}\n"
            f"• Whop revenue: ${totals['whop_revenue_usd']:.2f}",
        ),
        ("Open issues (auto-repair couldn't fix)", _open_issues(health_results)),
        ("External services", _external_quota_stubs()),
    ]

    sent = send_report(
        title=f"UGC Pipeline — Daily Health  ·  {pipeline_now().strftime('%Y-%m-%d')}",
        sections=sections,
    )
    log.info("daily health report sent", extra={"discord_sent": sent})
    return {
        "sections": [{"name": h, "body": b} for h, b in sections],
        "discord_sent": sent,
        "health_results_count": len(health_results),
    }
