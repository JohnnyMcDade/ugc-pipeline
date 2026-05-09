"""Agent 1: Trend & product scout.

For affiliate accounts (sharpguylab, rideupgrades) it scrapes the TikTok
Creative Center, applies persona-aware scoring via Claude, and writes the
top-N picks to disk.

For @passivepoly it skips Creative Center entirely — the "trends" come from
the live PassivePoly backend (whale alerts, win/loss, market resolutions).

Output (idempotent per day):
  data/trends/<handle>/<YYYY-MM-DD>/products.json    (affiliate)
  data/trends/<handle>/<YYYY-MM-DD>/signals.json     (passivepoly)
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agents.scout.scorer import score_products, select_passivepoly_signals
from core.config_loader import AccountConfig
from core.dateutils import today_str
from core.logger import get_logger
from integrations.claude_api import ClaudeClient
from integrations.passivepoly_backend import PassivePolyBackend
from integrations.tiktok_creative_center import CreativeCenterClient

OUTPUT_ROOT = Path("data/trends")


def _today_dir(handle: str) -> Path:
    d = OUTPUT_ROOT / handle / today_str()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def _master_scout_config() -> dict[str, Any]:
    # Imported lazily to avoid a circular import chain on rare reload paths.
    from core.config_loader import load_master
    return load_master(Path("config/master.yaml")).scout


def run(account: AccountConfig, ctx: dict[str, Any]) -> dict[str, Any]:
    log = get_logger("scout", account.handle)
    out_dir = _today_dir(account.handle)

    master_scout = _master_scout_config()
    weights = master_scout["scoring"]["weights"]
    min_score = master_scout["scoring"]["min_score"]
    output_top_n = master_scout["output_top_n"]

    # PassivePoly path: pull from live backend, score signals.
    if account.scout.get("source") == "passivepoly_backend":
        result = _run_passivepoly(account, output_top_n, log)
        out_path = out_dir / "signals.json"
        _write_json(out_path, result)
        log.info("scout complete", extra={"out": str(out_path), "n": len(result["signals"])})
        return result

    # Affiliate path: Creative Center → score → top N.
    result = _run_affiliate(account, master_scout, weights, min_score, output_top_n, log)
    out_path = out_dir / "products.json"
    _write_json(out_path, result)
    log.info("scout complete", extra={"out": str(out_path), "n": len(result["products"])})
    return result


def _run_affiliate(
    account: AccountConfig,
    master_scout: dict[str, Any],
    weights: dict[str, float],
    min_score: float,
    output_top_n: int,
    log,
) -> dict[str, Any]:
    cc = CreativeCenterClient(
        region=master_scout["creative_center"]["region"],
        period_days=master_scout["creative_center"]["period_days"],
        min_post_count=master_scout["creative_center"]["min_post_count"],
    )

    raw_products = cc.search_products(
        keywords=account.scout["keywords"],
        exclude_keywords=account.scout.get("exclude_keywords", []),
        price_range=account.scout.get("preferred_price_range_usd"),
    )
    log.info("creative center fetch", extra={"raw_count": len(raw_products)})

    if not raw_products:
        return {"account": account.handle, "products": [], "warning": "no products returned"}

    claude = ClaudeClient()
    # Use the cheap+fast tier for scoring — high volume, structured output.
    from core.config_loader import load_master
    model = load_master(Path("config/master.yaml")).models["claude"]["fast"]

    scored = score_products(
        account=account,
        products=raw_products,
        weights=weights,
        claude=claude,
        model=model,
    )
    accepted = [p for p in scored if p["score"] >= min_score][:output_top_n]
    rejected_count = len(scored) - len(accepted)
    log.info(
        "scout selection",
        extra={"accepted": len(accepted), "rejected": rejected_count, "min_score": min_score},
    )

    return {
        "account": account.handle,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "weights": weights,
        "min_score": min_score,
        "products": accepted,
        "rejected_count": rejected_count,
    }


def _run_passivepoly(
    account: AccountConfig,
    output_top_n: int,
    log,
) -> dict[str, Any]:
    creds = account.raw.get("api_credentials") or {}
    backend = PassivePolyBackend(
        base_url=creds.get("passivepoly_backend_url", ""),
        token=creds.get("passivepoly_backend_token", ""),
    )

    pull = account.scout["data_pull"]
    backend_data: dict[str, Any] = {}
    if pull.get("daily_alerts"):
        backend_data["daily_alerts"] = backend.daily_alerts()
    if pull.get("win_loss_last_7d"):
        backend_data["win_loss_7d"] = backend.win_loss(days=7)
    if pull.get("biggest_whale_move_24h"):
        backend_data["biggest_whale_move_24h"] = backend.biggest_whale_move(hours=24)
    if pull.get("notable_market_resolution"):
        backend_data["notable_resolution"] = backend.notable_resolution()
    log.info("passivepoly backend fetch", extra={"keys": list(backend_data)})

    claude = ClaudeClient()
    from core.config_loader import load_master
    model = load_master(Path("config/master.yaml")).models["claude"]["fast"]

    signals = select_passivepoly_signals(
        account=account,
        backend_data=backend_data,
        content_mix=account.scout["content_mix"],
        output_top_n=output_top_n,
        claude=claude,
        model=model,
    )

    return {
        "account": account.handle,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "content_mix": account.scout["content_mix"],
        "backend_snapshot": backend_data,
        "signals": signals,
    }
