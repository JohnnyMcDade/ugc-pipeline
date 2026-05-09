"""Scoring logic for the scout agent.

For affiliate niches: takes raw Creative Center products + the account persona,
asks Claude to score each on four axes, then computes the final weighted score
locally (not by Claude — keeps the math deterministic + auditable).

For passivepoly: takes the backend snapshot and asks Claude to pick & score
signals that fit the daily content mix.
"""

from __future__ import annotations

import json
from typing import Any

from agents.scout.prompts import (
    PASSIVEPOLY_SIGNAL_SYSTEM,
    PRODUCT_SCORING_SYSTEM,
    passivepoly_signal_user_prompt,
    product_scoring_user_prompt,
)
from core.config_loader import AccountConfig
from core.logger import get_logger
from integrations.claude_api import ClaudeClient


def score_products(
    account: AccountConfig,
    products: list[dict[str, Any]],
    weights: dict[str, float],
    claude: ClaudeClient,
    model: str,
) -> list[dict[str, Any]]:
    """Returns products augmented with axis scores + a final `score` float,
    sorted descending by `score`.
    """
    log = get_logger("scout.scorer", account.handle)
    if not products:
        log.info("no products to score")
        return []

    user_prompt = product_scoring_user_prompt(
        persona=account.persona,
        niche=account.niche,
        products=products,
        weights=weights,
    )
    raw = claude.complete_json(
        model=model,
        system=PRODUCT_SCORING_SYSTEM,
        user=user_prompt,
    )
    scored_index = {row["product_id"]: row for row in raw.get("scored", [])}

    out: list[dict[str, Any]] = []
    for p in products:
        row = scored_index.get(p["product_id"])
        if not row:
            log.warning("product missing from claude response", extra={"product_id": p["product_id"]})
            continue
        final = (
            weights["velocity"] * row["velocity"]
            + weights["relevance"] * row["relevance"]
            + weights["commission"] * row["commission"]
            - weights["saturation_penalty"] * row["saturation_penalty"]
        )
        out.append({
            **p,
            "axis_scores": {
                "velocity": row["velocity"],
                "relevance": row["relevance"],
                "commission": row["commission"],
                "saturation_penalty": row["saturation_penalty"],
            },
            "score": round(final, 4),
            "rationale": row.get("rationale", ""),
            "hook_angle": row.get("hook_angle", ""),
        })

    out.sort(key=lambda r: r["score"], reverse=True)
    log.info("scored products", extra={"count": len(out), "top_score": out[0]["score"] if out else None})
    return out


def select_passivepoly_signals(
    account: AccountConfig,
    backend_data: dict[str, Any],
    content_mix: dict[str, float],
    output_top_n: int,
    claude: ClaudeClient,
    model: str,
) -> list[dict[str, Any]]:
    log = get_logger("scout.scorer", account.handle)
    user_prompt = passivepoly_signal_user_prompt(
        persona=account.persona,
        backend_data=backend_data,
        content_mix=content_mix,
        output_top_n=output_top_n,
    )
    raw = claude.complete_json(
        model=model,
        system=PASSIVEPOLY_SIGNAL_SYSTEM,
        user=user_prompt,
    )
    signals = raw.get("signals", [])
    signals.sort(key=lambda s: s.get("score", 0.0), reverse=True)
    log.info(
        "selected passivepoly signals",
        extra={"count": len(signals), "categories": [s.get("category") for s in signals]},
    )
    return signals
