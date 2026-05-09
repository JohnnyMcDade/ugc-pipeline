"""Agent 8: Performance monitor (orchestrator).

Per account, per evening:

  1. Walk the published_log over the lookback window. Collect every
     successfully-posted (video_id, tiktok_post_id, posted_at).
  2. Join each entry to its script (hook, hook_type, source_pattern_id,
     source_product_id / source_signal_id, hashtags).
  3. Pull TikTok per-video metrics (views, likes, comments, shares, ER, ...).
  4. Pull TikTok Shop affiliate data (per-product + per-video clicks +
     revenue) — affiliate accounts only.
  5. Pull Whop subscription stats — passivepoly only.
  6. Compute winners (optimizer) → write winners.json (consumed by Agent 2
     tomorrow morning).
  7. Compute losers + kill candidates (killer) → write losers.json + merge
     into cumulative exclusions/{products,patterns}.json.
  8. Write per_video.json + report.json + report.md.
  9. Refresh the global cross-account summary.

All fetches are best-effort: if a per-video metric fetch errors, the row is
included with `error` set so the report still gets written. The TikTok
analytics + shop affiliate transports are intentionally stubs in their
integration modules — Agent 8 will run end-to-end the moment those are wired.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from agents.monitor import killer, optimizer
from core.config_loader import AccountConfig, load_master
from core.dateutils import today_str
from core.logger import get_logger
from integrations.tiktok_analytics import TikTokAnalyticsClient, TikTokAnalyticsError
from integrations.tiktok_shop_affiliate import (
    TikTokShopAffiliateClient,
    TikTokShopAffiliateError,
)
from integrations.whop_client import WhopAPIError, WhopClient

PUBLISHED_LOG_ROOT = Path("data/published_log")
SCRIPTS_ROOT = Path("data/scripts")
ANALYTICS_ROOT = Path("data/analytics")


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


# --- 1+2. Walk published_log + join scripts -----------------------------------

def _collect_posted(handle: str, lookback_days: int) -> list[dict[str, Any]]:
    today = datetime.now(tz=timezone.utc)
    out: list[dict[str, Any]] = []
    for offset in range(lookback_days + 1):
        date_str = (today - timedelta(days=offset)).strftime("%Y-%m-%d")
        day_dir = PUBLISHED_LOG_ROOT / handle / date_str
        if not day_dir.is_dir():
            continue
        for f in sorted(day_dir.glob("*.json")):
            if f.name == "manifest.json":
                continue
            doc = _read_json(f)
            if not doc or doc.get("publish_status") not in {"PUBLISH_COMPLETE", "PUBLISHED"}:
                continue
            out.append({"posted_date": date_str, "log": doc})
    return out


def _script_for(handle: str, posted_date: str, video_id: str) -> dict[str, Any] | None:
    doc = _read_json(SCRIPTS_ROOT / handle / posted_date / "scripts.json")
    if not doc:
        return None
    for s in doc.get("scripts", []):
        if s.get("video_id") == video_id:
            return s
    return None


def _patterns_for(handle: str, posted_date: str) -> dict[str, str]:
    doc = _read_json(Path("data/hooks") / handle / posted_date / "patterns.json")
    if not doc:
        return {}
    return {
        p["id"]: p.get("category", "other")
        for p in doc.get("patterns", [])
        if "id" in p
    }


def _hours_since(iso: str | None) -> float:
    if not iso:
        return 0.0
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(tz=timezone.utc) - dt).total_seconds() / 3600.0


# --- 3. TikTok per-video metrics ---------------------------------------------

def _fetch_video_metrics(
    handle: str,
    posted: list[dict[str, Any]],
    access_token: str,
    log,
) -> dict[str, dict[str, Any]]:
    """Returns {tiktok_post_id: metrics_row}. Each row includes `error` field
    if the fetch failed, so the per-video build can still proceed.
    """
    post_ids: list[str] = []
    for p in posted:
        for pid in (p["log"].get("tiktok_post_ids") or []):
            post_ids.append(pid)
    if not post_ids:
        return {}

    try:
        client = TikTokAnalyticsClient(access_token)
        rows = client.video_metrics(post_ids)
    except TikTokAnalyticsError as e:
        log.warning("tiktok analytics not wired", extra={"err": str(e)})
        return {pid: {"post_id": pid, "error": str(e), "metrics": {}} for pid in post_ids}

    return {row["post_id"]: row for row in rows}


# --- 4. Shop affiliate stats --------------------------------------------------

def _fetch_shop_data(
    account: AccountConfig,
    posted: list[dict[str, Any]],
    lookback_days: int,
    log,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Returns (per_video_shop, account_totals)."""
    if account.monetization.get("type") != "tiktok_shop_affiliate":
        return {}, {}

    creds = account.raw.get("api_credentials") or {}
    affiliate_id = creds.get("tiktok_shop_affiliate_id", "")
    access_token = creds.get("tiktok_session", "")

    today = datetime.now(tz=timezone.utc)
    start = (today - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    end = today.strftime("%Y-%m-%d")

    post_ids: list[str] = []
    for p in posted:
        for pid in (p["log"].get("tiktok_post_ids") or []):
            post_ids.append(pid)

    try:
        client = TikTokShopAffiliateClient(affiliate_id, access_token)
        per_video = client.per_video(start_date=start, end_date=end, post_ids=post_ids)
        totals = client.account_totals(start_date=start, end_date=end)
        return per_video, totals
    except TikTokShopAffiliateError as e:
        log.warning("shop affiliate not wired", extra={"err": str(e)})
        return {}, {}


# --- 5. Whop subscription stats (passivepoly) --------------------------------

def _fetch_whop(account: AccountConfig, lookback_days: int, log) -> dict[str, Any]:
    if account.monetization.get("type") != "subscription":
        return {}
    today = datetime.now(tz=timezone.utc)
    start_iso = (today - timedelta(days=lookback_days)).isoformat()
    end_iso = today.isoformat()
    try:
        client = WhopClient()
        return client.stats_for_window(start_iso=start_iso, end_iso=end_iso)
    except (WhopAPIError, RuntimeError) as e:
        log.warning("whop fetch failed", extra={"err": str(e)})
        return {"error": str(e)}


# --- 6+7. Build per_video rows -----------------------------------------------

def _build_per_video(
    handle: str,
    posted: list[dict[str, Any]],
    metrics_by_post_id: dict[str, dict[str, Any]],
    shop_by_post_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in posted:
        log_doc = p["log"]
        video_id = log_doc.get("video_id")
        post_ids = log_doc.get("tiktok_post_ids") or []
        primary_pid = post_ids[0] if post_ids else None

        script = _script_for(handle, p["posted_date"], video_id) or {}
        pat_cat = _patterns_for(handle, p["posted_date"])

        # Aggregate metrics across all post_ids (usually 1 per video).
        m_agg = {"views": 0, "likes": 0, "comments": 0, "shares": 0,
                 "profile_visits": 0, "watch_time_avg_seconds": 0.0,
                 "completion_rate": 0.0}
        m_count = 0
        m_errors: list[str] = []
        for pid in post_ids:
            row = metrics_by_post_id.get(pid) or {}
            if row.get("error"):
                m_errors.append(row["error"])
                continue
            m = row.get("metrics") or {}
            for k in ("views", "likes", "comments", "shares", "profile_visits"):
                m_agg[k] += int(m.get(k, 0))
            for k in ("watch_time_avg_seconds", "completion_rate"):
                m_agg[k] += float(m.get(k, 0.0))
            m_count += 1
        if m_count:
            m_agg["watch_time_avg_seconds"] = round(m_agg["watch_time_avg_seconds"] / m_count, 2)
            m_agg["completion_rate"] = round(m_agg["completion_rate"] / m_count, 4)

        views = max(1, m_agg["views"])
        engagement = m_agg["likes"] + m_agg["comments"] + m_agg["shares"]
        m_agg["engagement_rate"] = round(engagement / views, 5) if m_count else 0.0

        shop = {}
        if primary_pid:
            shop = shop_by_post_id.get(primary_pid, {})

        # Hook / hook_type from the source pattern's category.
        source_pattern_id = script.get("source_pattern_id")
        hook_type = pat_cat.get(source_pattern_id, "other") if source_pattern_id else "other"

        out.append({
            "video_id": video_id,
            "tiktok_post_ids": post_ids,
            "posted_at": log_doc.get("posted_at"),
            "age_hours": round(_hours_since(log_doc.get("posted_at")), 2),
            "metrics": m_agg,
            "metric_errors": m_errors,
            "shop": shop or {},
            "source": {
                "hook": script.get("hook"),
                "hook_type": hook_type,
                "source_pattern_id": source_pattern_id,
                "source_product_id": script.get("source_product_id"),
                "source_signal_id": script.get("source_signal_id"),
                "hashtags": log_doc.get("hashtags_final") or script.get("hashtags", []),
                "category": script.get("category"),
                "video_style": script.get("video_style"),
            },
        })
    return out


# --- 8. Reports ---------------------------------------------------------------

def _account_totals(per_video: list[dict[str, Any]]) -> dict[str, Any]:
    totals = {
        "videos_total": len(per_video),
        "views": 0, "likes": 0, "comments": 0, "shares": 0,
        "profile_visits": 0, "shop_clicks": 0, "shop_conversions": 0,
        "shop_revenue_usd": 0.0,
    }
    for v in per_video:
        m = v.get("metrics") or {}
        for k in ("views", "likes", "comments", "shares", "profile_visits"):
            totals[k] += int(m.get(k, 0))
        s = v.get("shop") or {}
        totals["shop_clicks"] += int(s.get("clicks", 0))
        totals["shop_conversions"] += int(s.get("conversions", 0))
        totals["shop_revenue_usd"] += float(s.get("revenue_usd", 0.0))
    totals["shop_revenue_usd"] = round(totals["shop_revenue_usd"], 2)
    if totals["views"] > 0:
        totals["avg_engagement_rate"] = round(
            (totals["likes"] + totals["comments"] + totals["shares"]) / totals["views"], 5
        )
    else:
        totals["avg_engagement_rate"] = 0.0
    return totals


def _render_markdown_report(
    handle: str,
    per_video: list[dict[str, Any]],
    account_totals: dict[str, Any],
    shop_totals: dict[str, Any],
    whop_stats: dict[str, Any],
    winners: list[dict[str, Any]],
    losers: list[dict[str, Any]],
    killed_patterns: list[dict[str, Any]],
    killed_products: list[dict[str, Any]],
    lookback_days: int,
) -> str:
    today = today_str()
    lines: list[str] = [
        f"# @{handle} — performance report",
        f"_{today} · lookback {lookback_days}d · {len(per_video)} videos_",
        "",
        "## Aggregate",
        f"- Views: **{account_totals['views']:,}**",
        f"- Likes: {account_totals['likes']:,}  ·  Comments: {account_totals['comments']:,}  ·  Shares: {account_totals['shares']:,}",
        f"- Avg engagement rate: **{account_totals['avg_engagement_rate']:.4f}**",
        f"- Profile visits: {account_totals['profile_visits']:,}",
    ]
    if shop_totals:
        lines += [
            "",
            "## TikTok Shop",
            f"- Clicks: {shop_totals.get('clicks', 0):,}",
            f"- Conversions: {shop_totals.get('conversions', 0):,}",
            f"- Revenue: **${shop_totals.get('revenue_usd', 0.0):,.2f}**",
        ]
    if whop_stats and "error" not in whop_stats:
        lines += [
            "",
            "## Whop (subscriptions)",
            f"- New trial signups: {whop_stats.get('trial_signups', 0)}",
            f"- New paid signups: **{whop_stats.get('paid_signups', 0)}**",
            f"- Revenue (window): **${whop_stats.get('revenue_usd', 0.0):,.2f}**",
            f"- Total memberships in window: {whop_stats.get('memberships_total', 0)}",
        ]
    elif whop_stats.get("error"):
        lines += ["", "## Whop", f"- ⚠️ fetch failed: {whop_stats['error']}"]

    lines += ["", "## Top videos"]
    top = sorted(per_video, key=lambda v: float((v.get("metrics") or {}).get("engagement_rate", 0.0)), reverse=True)[:5]
    if not top:
        lines.append("- (none)")
    for i, v in enumerate(top, 1):
        m = v.get("metrics") or {}
        src = v.get("source") or {}
        hook = (src.get("hook") or "").strip() or "(hook unknown)"
        lines.append(
            f"{i}. \"{hook[:80]}\" — ER {m.get('engagement_rate', 0):.4f}, "
            f"{m.get('views', 0):,} views"
        )

    lines += ["", "## Winners (fed to Agent 2 tomorrow)"]
    if not winners:
        lines.append("- (none above threshold)")
    for w in winners:
        lines.append(
            f"- {w.get('hook_type', 'other')} · ER {w['engagement_rate']:.4f} · "
            f"{w['view_count']:,} views — \"{(w.get('hook') or '')[:80]}\""
        )

    lines += ["", "## Losers"]
    if not losers:
        lines.append("- (none)")
    for l in losers:
        lines.append(
            f"- video {l.get('video_id', '?')[:8]} · ER {l['engagement_rate']:.4f} · "
            f"{l['view_count']:,} views · {l['age_hours']:.0f}h old · {l['reason']}"
        )

    if killed_patterns:
        lines += ["", "## Killed patterns (cumulative)"]
        for k in killed_patterns:
            lines.append(f"- `{k['pattern_id']}` — {k['reason']}")
    if killed_products:
        lines += ["", "## Killed products (cumulative)"]
        for k in killed_products:
            lines.append(f"- `{k['product_id']}` — {k['reason']}")

    return "\n".join(lines) + "\n"


# --- 9. Global summary --------------------------------------------------------

def _refresh_global_summary() -> dict[str, Any]:
    """Read each account's report.json for today and combine into one global
    document. Idempotent — last account to run produces the most complete view.
    """
    today = today_str()
    account_dirs = [d for d in ANALYTICS_ROOT.iterdir() if d.is_dir() and d.name != "_global"] if ANALYTICS_ROOT.is_dir() else []
    accounts: list[dict[str, Any]] = []
    for ad in account_dirs:
        report = _read_json(ad / today / "report.json")
        if report:
            accounts.append({"account": ad.name, **report})

    totals = {"views": 0, "likes": 0, "comments": 0, "shares": 0,
              "shop_revenue_usd": 0.0, "whop_revenue_usd": 0.0}
    for a in accounts:
        at = a.get("account_totals") or {}
        for k in ("views", "likes", "comments", "shares"):
            totals[k] += int(at.get(k, 0))
        totals["shop_revenue_usd"] += float((a.get("shop_totals") or {}).get("revenue_usd", 0.0))
        totals["whop_revenue_usd"] += float((a.get("whop_stats") or {}).get("revenue_usd", 0.0))
    totals["shop_revenue_usd"] = round(totals["shop_revenue_usd"], 2)
    totals["whop_revenue_usd"] = round(totals["whop_revenue_usd"], 2)
    totals["total_revenue_usd"] = round(totals["shop_revenue_usd"] + totals["whop_revenue_usd"], 2)

    summary = {
        "date": today,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "accounts": accounts,
        "totals": totals,
    }
    out_dir = ANALYTICS_ROOT / "_global" / today
    _write_json(out_dir / "summary.json", summary)

    md = [
        f"# global summary — {today}",
        "",
        f"- Total revenue: **${totals['total_revenue_usd']:,.2f}** "
        f"(shop ${totals['shop_revenue_usd']:,.2f} + whop ${totals['whop_revenue_usd']:,.2f})",
        f"- Total views: {totals['views']:,}",
        f"- Total interactions: {totals['likes'] + totals['comments'] + totals['shares']:,}",
        "",
        "| Account | Videos | Views | Avg ER | Revenue |",
        "|---|---:|---:|---:|---:|",
    ]
    for a in accounts:
        at = a.get("account_totals") or {}
        rev = float((a.get("shop_totals") or {}).get("revenue_usd", 0.0)) + float(
            (a.get("whop_stats") or {}).get("revenue_usd", 0.0)
        )
        md.append(
            f"| @{a['account']} | {at.get('videos_total', 0)} | {at.get('views', 0):,} | "
            f"{at.get('avg_engagement_rate', 0):.4f} | ${rev:,.2f} |"
        )
    (out_dir / "summary.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    return summary


# --- entry point --------------------------------------------------------------

def run(account: AccountConfig, ctx: dict[str, Any]) -> dict[str, Any]:
    log = get_logger("monitor", account.handle)
    master = load_master(Path("config/master.yaml"))
    cfg = master.monitor

    lookback = int(cfg.get("lookback_days", 7))
    win_cfg = cfg.get("winner_thresholds", {})
    lose_cfg = cfg.get("loser_thresholds", {})
    kill_cfg = cfg.get("kill_thresholds", {})

    today_dir = ANALYTICS_ROOT / account.handle / today_str()
    today_dir.mkdir(parents=True, exist_ok=True)

    # 1. Walk published_log.
    posted = _collect_posted(account.handle, lookback)
    log.info("collected posted videos", extra={"count": len(posted), "lookback_days": lookback})
    if not posted:
        empty = {"account": account.handle, "warning": "no_posted_videos_in_window"}
        _write_json(today_dir / "report.json", empty)
        return empty

    # 2+3. Metrics fetch.
    creds = account.raw.get("api_credentials") or {}
    metrics_by_post_id = _fetch_video_metrics(
        account.handle, posted, creds.get("tiktok_session", ""), log
    )

    # 4. Shop affiliate (affiliate accounts only).
    shop_by_post_id, shop_totals = _fetch_shop_data(account, posted, lookback, log)

    # 5. Whop (passivepoly only).
    whop_stats = _fetch_whop(account, lookback, log)

    # 6. Per-video join.
    per_video = _build_per_video(account.handle, posted, metrics_by_post_id, shop_by_post_id)
    _write_json(today_dir / "per_video.json", {
        "account": account.handle,
        "as_of": datetime.now(tz=timezone.utc).isoformat(),
        "lookback_days": lookback,
        "videos": per_video,
    })

    # 7. Winners.
    winners = optimizer.identify_winners(
        per_video=per_video,
        min_engagement_rate=float(win_cfg.get("min_engagement_rate", 0.05)),
        min_view_count=int(win_cfg.get("min_view_count", 50000)),
        take_top_n=int(win_cfg.get("take_top_n", 8)),
    )
    _write_json(today_dir / "winners.json", {
        "account": account.handle,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "winners": winners,
    })

    # 8. Losers + kill candidates + cumulative exclusions.
    losers = killer.identify_losers(
        per_video=per_video,
        max_engagement_rate=float(lose_cfg.get("max_engagement_rate", 0.02)),
        min_age_hours=float(lose_cfg.get("min_age_hours", 24)),
        min_view_floor=int(lose_cfg.get("min_view_floor", 1000)),
    )
    pattern_perf = killer.aggregate_pattern_performance(per_video)
    product_perf = killer.aggregate_product_performance(per_video)
    new_killed_patterns = killer.patterns_to_kill(
        pattern_perf,
        consecutive_uses_threshold=int(kill_cfg.get("consecutive_uses_to_kill_pattern", 3)),
        max_engagement_rate=float(lose_cfg.get("max_engagement_rate", 0.02)),
    )
    new_killed_products = killer.products_to_kill(
        product_perf,
        consecutive_uses_threshold=int(kill_cfg.get("consecutive_uses_to_kill_product", 3)),
    )

    excl_dir = ANALYTICS_ROOT / account.handle / "exclusions"
    existing_pat = (_read_json(excl_dir / "patterns.json") or {}).get("killed", [])
    existing_prod = (_read_json(excl_dir / "products.json") or {}).get("killed", [])
    cum_pat = killer.merge_exclusions(existing_pat, new_killed_patterns, key="pattern_id")
    cum_prod = killer.merge_exclusions(existing_prod, new_killed_products, key="product_id")
    _write_json(excl_dir / "patterns.json", {
        "killed": cum_pat,
        "updated_at": datetime.now(tz=timezone.utc).isoformat(),
    })
    _write_json(excl_dir / "products.json", {
        "killed": cum_prod,
        "updated_at": datetime.now(tz=timezone.utc).isoformat(),
    })
    _write_json(today_dir / "losers.json", {
        "account": account.handle,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "losers": losers,
        "newly_killed_patterns": new_killed_patterns,
        "newly_killed_products": new_killed_products,
    })

    # 9. Reports.
    account_totals = _account_totals(per_video)
    report = {
        "account": account.handle,
        "date": today_str(),
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "lookback_days": lookback,
        "account_totals": account_totals,
        "shop_totals": shop_totals,
        "whop_stats": whop_stats,
        "winners_count": len(winners),
        "losers_count": len(losers),
        "newly_killed_patterns_count": len(new_killed_patterns),
        "newly_killed_products_count": len(new_killed_products),
    }
    _write_json(today_dir / "report.json", report)

    if cfg.get("report", {}).get("write_markdown", True):
        md = _render_markdown_report(
            account.handle, per_video, account_totals, shop_totals, whop_stats,
            winners, losers, new_killed_patterns, new_killed_products, lookback,
        )
        (today_dir / "report.md").write_text(md, encoding="utf-8")

    if cfg.get("report", {}).get("write_global_summary", True):
        try:
            _refresh_global_summary()
        except Exception as e:
            log.warning("global summary refresh failed", extra={"err": str(e)})

    log.info("monitor complete", extra={
        "videos": len(per_video),
        "winners": len(winners),
        "losers": len(losers),
        "killed_patterns_today": len(new_killed_patterns),
        "killed_products_today": len(new_killed_products),
    })
    return report
