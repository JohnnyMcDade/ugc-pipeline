"""Agent 4: Video prompt engineer.

Single-platform now (HeyGen for all 3 accounts). Reads validated scripts
from Agent 3, joins each script's `source_pattern_id` to today's hook
patterns to inject `pattern_category` (used by the formatter to pick avatar
style + voice speed), then formats the HeyGen v2 payload deterministically.

No LLM call here — HeyGen's API is structured. (The previous Higgsfield path
needed Claude expansion of broll concepts; HeyGen does not.)

Output:
  data/video_prompts/<handle>/<YYYY-MM-DD>/<video_id>.json   (per script)
  data/video_prompts/<handle>/<YYYY-MM-DD>/manifest.json     (index)
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agents.videoprompt.heygen_formatter import format_heygen_payload
from core.config_loader import AccountConfig
from core.dateutils import today_str
from core.logger import get_logger

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
    data = _read_json(HOOKS_ROOT / handle / today_str() / "patterns.json")
    if not data:
        return {}
    return {p["id"]: p.get("category", "other") for p in data.get("patterns", []) if "id" in p}


def run(account: AccountConfig, ctx: dict[str, Any]) -> dict[str, Any]:
    log = get_logger("videoprompt", account.handle)
    out_dir = _today_dir(account.handle)

    scripts_doc = _load_today_scripts(account.handle)
    if not scripts_doc:
        log.warning("no scripts.json for today")
        manifest = {"account": account.handle, "items": [], "warning": "no_scripts_today"}
        _write_json(out_dir / "manifest.json", manifest)
        return manifest

    scripts = [s for s in scripts_doc.get("scripts", []) if s.get("validation", {}).get("passed")]
    if not scripts:
        log.warning("no validated scripts")
        manifest = {"account": account.handle, "items": [], "warning": "no_valid_scripts"}
        _write_json(out_dir / "manifest.json", manifest)
        return manifest

    pat_cat = _pattern_category_index(account.handle)
    items: list[dict[str, Any]] = []
    formatted = 0

    for script in scripts:
        if script.get("source_pattern_id") in pat_cat:
            script["pattern_category"] = pat_cat[script["source_pattern_id"]]

        out_path = out_dir / f"{script['video_id']}.json"
        try:
            payload = format_heygen_payload(script, account)
            _write_json(out_path, payload)
            items.append({"video_id": script["video_id"], "platform": "heygen", "path": out_path.name})
            formatted += 1
        except Exception as e:
            log.exception("format failed", extra={"video_id": script.get("video_id"), "err": str(e)})

    manifest = {
        "account": account.handle,
        "video_style": account.video_style,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "items": items,
        "formatted_count": formatted,
    }
    _write_json(out_dir / "manifest.json", manifest)
    log.info("videoprompt complete", extra={"formatted": formatted, "total": len(items)})
    return manifest
