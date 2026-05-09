"""Agent 4: Video prompt engineer.

Converts validated scripts (Agent 3 output) into provider-specific prompt
payloads ready for Agent 5 to POST.

  arcads_avatar       → arcads_formatter.format_arcads_payload  (deterministic)
  higgsfield_lifestyle → Claude-assisted Higgsfield expansion (this module)

Idempotency: each script's video_id is the filename stem. If the file already
exists for today, we skip Claude (Higgsfield path) entirely. The Arcads path
is always cheap so we always rewrite it.

Output:
  data/video_prompts/<handle>/<YYYY-MM-DD>/<video_id>.json   (per script)
  data/video_prompts/<handle>/<YYYY-MM-DD>/manifest.json     (index)
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agents.videoprompt.arcads_formatter import format_arcads_payload
from agents.videoprompt.prompts import (
    HIGGSFIELD_EXPANSION_SYSTEM,
    higgsfield_expansion_user_prompt,
)
from core.config_loader import AccountConfig, load_master
from core.dateutils import today_str
from core.logger import get_logger
from integrations.claude_api import ClaudeClient

SCRIPTS_ROOT = Path("data/scripts")
HOOKS_ROOT = Path("data/hooks")
OUTPUT_ROOT = Path("data/video_prompts")


def _today_dir(handle: str) -> Path:
    d = OUTPUT_ROOT / handle / today_str()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def _load_today_scripts(handle: str) -> dict[str, Any] | None:
    return _read_json(SCRIPTS_ROOT / handle / today_str() / "scripts.json")


def _pattern_category_index(handle: str) -> dict[str, str]:
    """Maps pattern_id → category, read from today's hooks output. The Arcads
    formatter uses this to derive emotion/pacing from hook_type.
    """
    data = _read_json(HOOKS_ROOT / handle / today_str() / "patterns.json")
    if not data:
        return {}
    return {
        p["id"]: p.get("category", "other")
        for p in data.get("patterns", [])
        if "id" in p
    }


def _build_higgsfield(
    script: dict[str, Any],
    account: AccountConfig,
    claude: ClaudeClient,
    model: str,
) -> dict[str, Any]:
    higgs_cfg = (account.raw.get("videogen") or {}).get("higgsfield") or {}
    if not higgs_cfg.get("location_anchor") or not higgs_cfg.get("visual_vocabulary"):
        raise RuntimeError(
            f"@{account.handle}: videogen.higgsfield must define location_anchor "
            "and visual_vocabulary"
        )

    raw = claude.complete_json(
        model=model,
        system=HIGGSFIELD_EXPANSION_SYSTEM,
        user=higgsfield_expansion_user_prompt(
            script=script,
            higgsfield_cfg=higgs_cfg,
            target_duration_seconds=int(script.get("target_duration_seconds") or 22),
        ),
    )

    overlays = script.get("on_screen_overlays") or []
    segments = list(raw.get("segments") or [])
    # Length sanity: pad with the hero clip if Claude returned too few.
    while len(segments) < len(overlays):
        idx = len(segments)
        segments.append({
            "overlay_index": idx,
            "overlay_text": overlays[idx].get("text", ""),
            "prompt": (raw.get("hero_clip") or {}).get("prompt", ""),
            "duration_seconds": (raw.get("hero_clip") or {}).get("duration_seconds", 6),
            "camera": (raw.get("hero_clip") or {}).get("camera", "static medium shot"),
            "negative_prompt": (raw.get("hero_clip") or {}).get("negative_prompt", ""),
            "filled_from_hero": True,
        })
    # Trim if too many.
    segments = segments[: max(1, len(overlays))]

    return {
        "video_id": script["video_id"],
        "account": account.handle,
        "video_style": "higgsfield_lifestyle",
        "platform": "higgsfield",
        "aspect_ratio": higgs_cfg.get("aspect_ratio", "9:16"),
        "hero_clip": raw.get("hero_clip"),
        "segments": segments,
        "evidence_screenshot_required": bool(script.get("evidence_screenshot_required")),
        "evidence_payload": script.get("evidence_payload"),
        "metadata": {
            "source_script_video_id": script["video_id"],
            "source_signal_id": script.get("source_signal_id"),
            "source_pattern_id": script.get("source_pattern_id"),
            "category": script.get("category"),
            "target_duration_seconds": script.get("target_duration_seconds"),
            "caption": script.get("caption"),
            "hashtags": script.get("hashtags", []),
            "cta_url": script.get("cta_url"),
        },
    }


def run(account: AccountConfig, ctx: dict[str, Any]) -> dict[str, Any]:
    log = get_logger("videoprompt", account.handle)
    out_dir = _today_dir(account.handle)

    scripts_doc = _load_today_scripts(account.handle)
    if not scripts_doc:
        log.warning("no scripts.json for today — nothing to format")
        manifest = {"account": account.handle, "items": [], "warning": "no_scripts_today"}
        _write_json(out_dir / "manifest.json", manifest)
        return manifest

    scripts = [s for s in scripts_doc.get("scripts", []) if s.get("validation", {}).get("passed")]
    if not scripts:
        log.warning("no validated scripts for today")
        manifest = {"account": account.handle, "items": [], "warning": "no_valid_scripts"}
        _write_json(out_dir / "manifest.json", manifest)
        return manifest

    pat_cat = _pattern_category_index(account.handle)
    claude: ClaudeClient | None = None
    model: str | None = None

    items: list[dict[str, Any]] = []
    formatted_count = 0
    skipped_existing = 0

    for script in scripts:
        # Inject pattern_category so the Arcads formatter can derive emotion.
        if script.get("source_pattern_id") in pat_cat:
            script["pattern_category"] = pat_cat[script["source_pattern_id"]]

        out_path = out_dir / f"{script['video_id']}.json"

        try:
            if account.video_style == "arcads_avatar":
                # Always rewrite — deterministic and cheap.
                payload = format_arcads_payload(script, account)
                _write_json(out_path, payload)
                items.append({"video_id": script["video_id"], "platform": "arcads", "path": out_path.name})
                formatted_count += 1

            elif account.video_style == "higgsfield_lifestyle":
                if out_path.exists():
                    log.info("skipping (already formatted)", extra={"video_id": script["video_id"]})
                    items.append({
                        "video_id": script["video_id"],
                        "platform": "higgsfield",
                        "path": out_path.name,
                    })
                    skipped_existing += 1
                    continue
                if claude is None:
                    claude = ClaudeClient()
                    model = load_master(Path("config/master.yaml")).models["claude"]["primary"]
                payload = _build_higgsfield(script, account, claude, model)
                _write_json(out_path, payload)
                items.append({"video_id": script["video_id"], "platform": "higgsfield", "path": out_path.name})
                formatted_count += 1

            else:
                log.error("unknown video_style", extra={"video_style": account.video_style})

        except Exception as e:
            log.exception("format failed", extra={"video_id": script.get("video_id"), "err": str(e)})

    manifest = {
        "account": account.handle,
        "video_style": account.video_style,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "items": items,
        "formatted_count": formatted_count,
        "skipped_existing": skipped_existing,
    }
    _write_json(out_dir / "manifest.json", manifest)
    log.info(
        "videoprompt complete",
        extra={"formatted": formatted_count, "skipped_existing": skipped_existing, "total": len(items)},
    )
    return manifest
