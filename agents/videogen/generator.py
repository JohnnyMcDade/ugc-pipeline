"""Agent 5: Video generator (orchestrator).

Single-platform: HeyGen for all 3 accounts. For each video_id in today's
video_prompts manifest:

  1. Skip if `result.json` already exists with `qc_passed: true` (idempotent).
  2. Submit the HeyGen v2 generate body.
  3. Poll until terminal status.
  4. Download the mp4.
  5. Run quality_check (expects audio — avatar talks).
  6. Persist files + result.json.

Caps to `videogen.max_videos_per_account_per_day` from master.yaml.

Output:
  data/raw_videos/<handle>/<date>/<video_id>/heygen.mp4
  data/raw_videos/<handle>/<date>/<video_id>/result.json
  data/raw_videos/<handle>/<date>/manifest.json
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agents.videogen.poller import PollerError, poll_until_complete
from agents.videogen.quality_check import check_video
from core.config_loader import AccountConfig, load_master
from core.dateutils import today_str
from core.logger import get_logger
from integrations.heygen_client import HeyGenAPIError, HeyGenClient

PROMPTS_ROOT = Path("data/video_prompts")
RAW_VIDEOS_ROOT = Path("data/raw_videos")


def _today_dir(handle: str) -> Path:
    d = RAW_VIDEOS_ROOT / handle / today_str()
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


def _videogen_cfg() -> dict[str, Any]:
    return load_master(Path("config/master.yaml")).videogen


def _generate_one(
    prompt_doc: dict[str, Any],
    out_dir: Path,
    log,
) -> dict[str, Any]:
    cfg = _videogen_cfg()
    api_key = os.environ.get("HEYGEN_API_KEY", "")
    client = HeyGenClient(api_key=api_key, max_retries=int(cfg.get("max_retries", 2)))

    payload = prompt_doc["payload"]
    target_duration = int((prompt_doc.get("metadata") or {}).get("target_duration_seconds") or 30)

    log.info("heygen submit", extra={"video_id": prompt_doc["video_id"]})
    video_id_remote = client.submit_video(payload)

    status = poll_until_complete(
        job_id=video_id_remote,
        status_fn=client.get_video_status,
        interval_seconds=int(cfg.get("poll_interval_seconds", 30)),
        timeout_seconds=int(cfg.get("poll_timeout_seconds", 1200)),
        log=log,
        label="heygen",
    )

    url = status.get("video_url")
    if not url:
        raise HeyGenAPIError(200, f"completed but no video URL in status: {status}")

    dest = out_dir / "heygen.mp4"
    client.download_video(url, dest)
    qc = check_video(dest, expected_duration_seconds=target_duration, expect_audio=True)

    return {
        "files": [{
            "name": dest.name,
            "role": "main",
            "path": dest.name,
            "qc": qc,
            "heygen_video_id": video_id_remote,
            "heygen_thumbnail_url": status.get("thumbnail_url"),
        }],
        "qc_passed": qc["passed"],
        "duration_seconds_total": qc["info"].get("duration_s", 0.0),
    }


def run(account: AccountConfig, ctx: dict[str, Any]) -> dict[str, Any]:
    log = get_logger("videogen", account.handle)
    out_root = _today_dir(account.handle)
    cfg = _videogen_cfg()
    cap = int(cfg.get("max_videos_per_account_per_day", 4))

    manifest_in = _read_json(PROMPTS_ROOT / account.handle / today_str() / "manifest.json")
    if not manifest_in:
        log.warning("no video_prompts manifest")
        m = {"account": account.handle, "items": [], "warning": "no_prompts_manifest"}
        _write_json(out_root / "manifest.json", m)
        return m

    items = list(manifest_in.get("items", []))
    log.info("videogen start", extra={"queued": len(items), "cap": cap})

    out_items: list[dict[str, Any]] = []
    succeeded = 0
    failed = 0
    skipped_cap = 0

    for entry in items:
        if succeeded >= cap:
            log.info("daily cap reached", extra={"cap": cap})
            skipped_cap += 1
            out_items.append({**entry, "skipped_reason": "daily_cap"})
            continue

        video_id = entry["video_id"]
        video_dir = out_root / video_id
        video_dir.mkdir(parents=True, exist_ok=True)
        result_path = video_dir / "result.json"

        existing = _read_json(result_path)
        if existing and existing.get("qc_passed"):
            log.info("skipping (already generated + QC passed)", extra={"video_id": video_id})
            out_items.append({"video_id": video_id, "platform": "heygen", "qc_passed": True,
                              "path": f"{video_id}/result.json", "reused": True})
            succeeded += 1
            continue

        prompt_doc = _read_json(PROMPTS_ROOT / account.handle / today_str() / entry["path"])
        if not prompt_doc:
            log.error("missing prompt doc", extra={"video_id": video_id})
            failed += 1
            continue

        try:
            gen_result = _generate_one(prompt_doc, video_dir, log)

            result = {
                "video_id": video_id,
                "account": account.handle,
                "platform": "heygen",
                "generated_at": datetime.now(tz=timezone.utc).isoformat(),
                "files": gen_result["files"],
                "qc_passed": gen_result["qc_passed"],
                "duration_seconds_total": gen_result["duration_seconds_total"],
                "metadata": prompt_doc.get("metadata", {}),
                "evidence_screenshot_required": prompt_doc.get("evidence_screenshot_required", False),
                "evidence_payload": prompt_doc.get("evidence_payload"),
                "evidence_show_at_seconds": prompt_doc.get("evidence_show_at_seconds"),
                "evidence_show_duration_seconds": prompt_doc.get("evidence_show_duration_seconds"),
            }
            _write_json(result_path, result)

            out_items.append({
                "video_id": video_id,
                "platform": "heygen",
                "qc_passed": result["qc_passed"],
                "path": f"{video_id}/result.json",
            })
            if result["qc_passed"]:
                succeeded += 1
            else:
                failed += 1
            log.info("video produced", extra={
                "video_id": video_id, "qc_passed": result["qc_passed"],
                "duration_s": result["duration_seconds_total"],
            })

        except (HeyGenAPIError, PollerError) as e:
            log.exception("video generation failed", extra={"video_id": video_id, "err": str(e)})
            _write_json(result_path, {
                "video_id": video_id, "account": account.handle, "platform": "heygen",
                "generated_at": datetime.now(tz=timezone.utc).isoformat(),
                "qc_passed": False, "error": str(e),
                "metadata": prompt_doc.get("metadata", {}),
            })
            out_items.append({
                "video_id": video_id, "platform": "heygen", "qc_passed": False,
                "path": f"{video_id}/result.json", "error": str(e),
            })
            failed += 1
        except Exception as e:
            log.exception("unexpected error", extra={"video_id": video_id, "err": str(e)})
            failed += 1

    manifest_out = {
        "account": account.handle,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "items": out_items,
        "succeeded": succeeded,
        "failed": failed,
        "skipped_cap": skipped_cap,
        "max_per_day": cap,
    }
    _write_json(out_root / "manifest.json", manifest_out)
    log.info("videogen complete", extra={
        "succeeded": succeeded, "failed": failed, "skipped_cap": skipped_cap,
    })
    return manifest_out
