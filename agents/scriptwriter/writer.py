"""Agent 3: Script writer.

Reads today's scout output + today's hook patterns for an account, asks Claude
to write N persona-locked script variants in one batch, then validates each
one locally for banned phrases, duration, provenance, and CTA identity.

All accounts now produce talking-head avatar UGC (HeyGen). Branches on
account.monetization.type, not video_style:

  tiktok_shop_affiliate → SCRIPT_SYSTEM_AFFILIATE  (sharpguylab, rideupgrades)
  subscription          → SCRIPT_SYSTEM_PASSIVEPOLY (passivepoly)

Output: data/scripts/<handle>/<YYYY-MM-DD>/scripts.json
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agents.scriptwriter.personas import validate_script
from agents.scriptwriter.prompts import (
    SCRIPT_SYSTEM_AFFILIATE,
    SCRIPT_SYSTEM_PASSIVEPOLY,
    affiliate_user_prompt,
    passivepoly_user_prompt,
)
from core.config_loader import AccountConfig, load_master
from core.dateutils import today_str
from core.logger import get_logger
from integrations.claude_api import ClaudeClient

TRENDS_ROOT = Path("data/trends")
HOOKS_ROOT = Path("data/hooks")
SCRIPTS_ROOT = Path("data/scripts")


def _today_dir(handle: str) -> Path:
    d = SCRIPTS_ROOT / handle / today_str()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _load_today_trends(account: AccountConfig) -> tuple[list[dict[str, Any]], str]:
    """Returns (items, kind). `kind` is "products" or "signals" depending on
    which file the scout produced for this account today.
    """
    today = today_str()
    base = TRENDS_ROOT / account.handle / today
    products_path = base / "products.json"
    signals_path = base / "signals.json"
    if signals_path.exists():
        data = _read_json(signals_path) or {}
        return list(data.get("signals", [])), "signals"
    if products_path.exists():
        data = _read_json(products_path) or {}
        return list(data.get("products", [])), "products"
    return [], "none"


def _load_today_patterns(account: AccountConfig) -> list[dict[str, Any]]:
    path = HOOKS_ROOT / account.handle / today_str() / "patterns.json"
    data = _read_json(path) or {}
    return list(data.get("patterns", []))


def _fallback_patterns_from_persona(account: AccountConfig) -> list[dict[str, Any]]:
    """When Agent 2 produced no patterns (cold start, scraper not wired),
    seed the writer with the persona's own example_hooks from YAML.
    """
    examples = list(account.persona.get("example_hooks", []))
    return [
        {
            "id": f"persona_seed_{i}",
            "category": "other",
            "template": ex,
            "examples": [ex],
            "final_score": 0.5,
            "persona_fit_notes": "persona seed (Agent 2 produced no patterns today)",
        }
        for i, ex in enumerate(examples)
    ]


def _trim_for_prompt(items: list[dict[str, Any]], top_n: int) -> list[dict[str, Any]]:
    """Cap how many we send to Claude. Keeps token use bounded and forces
    selection from the strongest candidates.
    """
    return items[:top_n]


def run(account: AccountConfig, ctx: dict[str, Any]) -> dict[str, Any]:
    log = get_logger("scriptwriter", account.handle)
    out_dir = _today_dir(account.handle)

    master = load_master(Path("config/master.yaml"))
    sw_cfg = master.scriptwriter
    variants = int(sw_cfg.get("variants_per_account", 4))
    duration_tol = int(sw_cfg.get("duration_tolerance_seconds", 6))
    wpm = int(sw_cfg.get("words_per_minute", 150))
    max_hashtags = int(sw_cfg.get("max_hashtags", 6))
    drop_invalid = bool(sw_cfg.get("drop_invalid", True))
    target = int(
        sw_cfg.get("target_duration_seconds", {}).get(account.video_style, 30)
    )

    items, kind = _load_today_trends(account)
    if not items:
        log.warning("no trends for today — skipping script generation", extra={"kind": kind})
        result = _empty_result(account, reason="no_trends_today")
        (out_dir / "scripts.json").write_text(json.dumps(result, indent=2))
        return result

    patterns = _load_today_patterns(account)
    fallback_used = False
    if not patterns:
        patterns = _fallback_patterns_from_persona(account)
        fallback_used = True
        log.warning("no hook patterns today — falling back to persona seeds",
                    extra={"seed_count": len(patterns)})
        if not patterns:
            log.error("no patterns and no persona seeds — cannot write scripts")
            result = _empty_result(account, reason="no_patterns_or_seeds")
            (out_dir / "scripts.json").write_text(json.dumps(result, indent=2))
            return result

    # Cap inputs to keep token use & cost bounded.
    items_for_prompt = _trim_for_prompt(items, top_n=8)
    patterns_for_prompt = _trim_for_prompt(patterns, top_n=8)

    # Generate.
    claude = ClaudeClient()
    model = master.models["claude"]["primary"]  # quality matters more than cost here

    # All accounts now produce talking-head avatar UGC via HeyGen. The two
    # prompt families differ only by monetization: TikTok Shop affiliate vs.
    # subscription (passivepoly). Branch on monetization.type, not video_style.
    monetization_type = (account.monetization or {}).get("type", "")
    if monetization_type == "tiktok_shop_affiliate":
        raw = claude.complete_json(
            model=model,
            system=SCRIPT_SYSTEM_AFFILIATE,
            user=affiliate_user_prompt(
                persona=account.persona,
                niche=account.niche,
                monetization=account.monetization,
                target_duration_seconds=target,
                words_per_minute=wpm,
                max_hashtags=max_hashtags,
                variants=variants,
                products=items_for_prompt,
                patterns=patterns_for_prompt,
            ),
        )
        cta_url_required = None
    elif monetization_type == "subscription":
        raw = claude.complete_json(
            model=model,
            system=SCRIPT_SYSTEM_PASSIVEPOLY,
            user=passivepoly_user_prompt(
                persona=account.persona,
                target_duration_seconds=target,
                words_per_minute=wpm,
                max_hashtags=max_hashtags,
                variants=variants,
                signals=items_for_prompt,
                patterns=patterns_for_prompt,
                cta_url=account.monetization.get("cta_url", "https://passivepoly.com"),
            ),
        )
        cta_url_required = account.monetization.get("cta_url", "https://passivepoly.com")
    else:
        raise RuntimeError(f"unknown monetization.type: {monetization_type!r}")

    raw_variants = list(raw.get("variants", []))

    # Validate locally.
    valid_pattern_ids = {p["id"] for p in patterns_for_prompt if "id" in p}
    if kind == "products":
        valid_source_ids = {it["product_id"] for it in items_for_prompt if "product_id" in it}
    elif kind == "signals":
        valid_source_ids = {
            it.get("source_event_id") for it in items_for_prompt
            if it.get("source_event_id")
        }
    else:
        valid_source_ids = set()

    finalized: list[dict[str, Any]] = []
    dropped = 0
    for v in raw_variants:
        issues = validate_script(
            script=v,
            account=account,
            target_duration_seconds=target,
            duration_tolerance_seconds=duration_tol,
            words_per_minute=wpm,
            max_hashtags=max_hashtags,
            valid_pattern_ids=valid_pattern_ids,
            valid_source_ids=valid_source_ids,
            cta_url_required=cta_url_required,
        )
        v["video_id"] = str(uuid.uuid4())
        v["account"] = account.handle
        v["video_style"] = account.video_style
        v["validation"] = {"passed": not issues, "issues": issues}
        if issues and drop_invalid:
            log.info("dropped invalid variant",
                     extra={"variant_index": v.get("variant_index"), "issues": issues})
            dropped += 1
            continue
        finalized.append(v)

    log.info(
        "scriptwriter complete",
        extra={
            "kept": len(finalized),
            "dropped": dropped,
            "fallback_patterns": fallback_used,
            "trend_kind": kind,
        },
    )

    result = {
        "account": account.handle,
        "video_style": account.video_style,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "trend_kind": kind,
        "fallback_patterns_used": fallback_used,
        "target_duration_seconds": target,
        "scripts": finalized,
        "dropped_count": dropped,
    }
    out_path = out_dir / "scripts.json"
    out_path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    return result


def _empty_result(account: AccountConfig, reason: str) -> dict[str, Any]:
    return {
        "account": account.handle,
        "video_style": account.video_style,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "scripts": [],
        "warning": reason,
    }
