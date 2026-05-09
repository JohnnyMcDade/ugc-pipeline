"""Agent 5: Video generator (orchestrator).

For each video_id in today's video_prompts manifest:
  1. Skip if `result.json` already exists with `qc_passed: true` (idempotency).
  2. Branch on `platform`:
       arcads     → 1 submission   → 1 download
       higgsfield → 1 submission per (hero_clip + each segment) → N+1 downloads
  3. Poll each job to completion.
  4. QC each downloaded file (audio expected only for Arcads).
  5. Persist files + result.json under data/raw_videos/<handle>/<date>/<video_id>/.

Caps to `videogen.max_videos_per_account_per_day` from master.yaml. Anything
beyond the cap is skipped and noted in the manifest, not silently dropped.

Output:
  data/raw_videos/<handle>/<date>/<video_id>/{arcads.mp4 | hero_clip.mp4, segment_*.mp4}
  data/raw_videos/<handle>/<date>/<video_id>/result.json
  data/raw_videos/<handle>/<date>/manifest.json
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agents.videogen.arcads_client import ArcadsAPIError, ArcadsClient
from agents.videogen.higgsfield_client import HiggsfieldAPIError, HiggsfieldClient
from agents.videogen.poller import PollerError, poll_until_complete
from agents.videogen.quality_check import check_video
from core.config_loader import AccountConfig, load_master
from core.dateutils import today_str
from core.logger import get_logger

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


def _arcads_status_url_field(status: dict[str, Any]) -> str | None:
    return (
        status.get("video_url")
        or status.get("output_url")
        or (status.get("output") or {}).get("url")
    )


def _higgsfield_status_url_field(status: dict[str, Any]) -> str | None:
    return (
        status.get("video_url")
        or status.get("output_url")
        or (status.get("output") or {}).get("url")
        or (status.get("result") or {}).get("video_url")
    )


def _videogen_cfg() -> dict[str, Any]:
    return load_master(Path("config/master.yaml")).videogen


def _generate_arcads(
    prompt_doc: dict[str, Any],
    account: AccountConfig,
    out_dir: Path,
    log,
) -> dict[str, Any]:
    cfg = _videogen_cfg()
    creds = account.raw.get("api_credentials") or {}
    client = ArcadsClient(api_key=creds.get("arcads_key", ""), max_retries=int(cfg.get("max_retries", 2)))

    payload = prompt_doc["payload"]
    target_duration = int((prompt_doc.get("metadata") or {}).get("target_duration_seconds") or 30)

    log.info("arcads submit", extra={"video_id": prompt_doc["video_id"]})
    job_id = client.submit_video(payload)

    status = poll_until_complete(
        job_id=job_id,
        status_fn=client.get_video_status,
        interval_seconds=int(cfg.get("poll_interval_seconds", 30)),
        timeout_seconds=int(cfg.get("poll_timeout_seconds", 1200)),
        log=log,
        label="arcads",
    )

    url = _arcads_status_url_field(status)
    if not url:
        raise ArcadsAPIError(200, f"completed but no video URL in status: {status}")

    dest = out_dir / "arcads.mp4"
    client.download_video(url, dest)
    qc = check_video(dest, expected_duration_seconds=target_duration, expect_audio=True)

    return {
        "files": [{
            "name": dest.name,
            "role": "main",
            "path": dest.name,
            "qc": qc,
            "arcads_job_id": job_id,
        }],
        "qc_passed": qc["passed"],
        "duration_seconds_total": qc["info"].get("duration_s", 0.0),
    }


def _generate_higgsfield(
    prompt_doc: dict[str, Any],
    account: AccountConfig,
    out_dir: Path,
    log,
) -> dict[str, Any]:
    cfg = _videogen_cfg()
    creds = account.raw.get("api_credentials") or {}
    client = HiggsfieldClient(
        api_key=creds.get("higgsfield_key", ""),
        max_retries=int(cfg.get("max_retries", 2)),
    )

    aspect = prompt_doc.get("aspect_ratio", "9:16")
    files: list[dict[str, Any]] = []

    # The hero clip plus each segment becomes its own clip submission.
    clips_to_make: list[tuple[str, dict[str, Any], int | None]] = []
    if prompt_doc.get("hero_clip"):
        clips_to_make.append(("hero_clip", prompt_doc["hero_clip"], None))
    for seg in prompt_doc.get("segments", []):
        idx = seg.get("overlay_index", 0)
        clips_to_make.append((f"segment_{idx}", seg, idx))

    total_duration = 0.0
    qc_passed_all = True

    for fname_stem, clip_spec, overlay_index in clips_to_make:
        clip_spec_with_aspect = {**clip_spec, "aspect_ratio": aspect}
        log.info(
            "higgsfield submit",
            extra={"video_id": prompt_doc["video_id"], "clip": fname_stem},
        )
        try:
            job_id = client.submit_clip(clip_spec_with_aspect)
            status = poll_until_complete(
                job_id=job_id,
                status_fn=client.get_clip_status,
                interval_seconds=int(cfg.get("poll_interval_seconds", 30)),
                timeout_seconds=int(cfg.get("poll_timeout_seconds", 1200)),
                log=log,
                label=f"higgsfield:{fname_stem}",
            )
            url = _higgsfield_status_url_field(status)
            if not url:
                raise HiggsfieldAPIError(200, f"completed but no video URL: {status}")

            dest = out_dir / f"{fname_stem}.mp4"
            client.download_clip(url, dest)
            qc = check_video(
                dest,
                expected_duration_seconds=float(clip_spec.get("duration_seconds", 6)),
                expect_audio=False,
            )
            qc_passed_all = qc_passed_all and qc["passed"]
            total_duration += qc["info"].get("duration_s", 0.0)

            files.append({
                "name": dest.name,
                "role": "hero" if overlay_index is None else "segment",
                "overlay_index": overlay_index,
                "path": dest.name,
                "prompt": clip_spec.get("prompt"),
                "qc": qc,
                "higgsfield_job_id": job_id,
            })
        except (HiggsfieldAPIError, PollerError) as e:
            log.error(
                "higgsfield clip failed",
                extra={"video_id": prompt_doc["video_id"], "clip": fname_stem, "err": str(e)},
            )
            qc_passed_all = False
            files.append({
                "name": f"{fname_stem}.mp4",
                "role": "hero" if overlay_index is None else "segment",
                "overlay_index": overlay_index,
                "error": str(e),
                "qc": {"passed": False, "issues": [str(e)]},
            })

    return {
        "files": files,
        "qc_passed": qc_passed_all,
        "duration_seconds_total": round(total_duration, 2),
    }


def run(account: AccountConfig, ctx: dict[str, Any]) -> dict[str, Any]:
    log = get_logger("videogen", account.handle)
    out_root = _today_dir(account.handle)
    cfg = _videogen_cfg()
    cap = int(cfg.get("max_videos_per_account_per_day", 4))

    manifest_in = _read_json(PROMPTS_ROOT / account.handle / today_str() / "manifest.json")
    if not manifest_in:
        log.warning("no video_prompts manifest for today")
        manifest_out = {"account": account.handle, "items": [], "warning": "no_prompts_manifest"}
        _write_json(out_root / "manifest.json", manifest_out)
        return manifest_out

    items = list(manifest_in.get("items", []))
    log.info("videogen start", extra={"queued": len(items), "cap": cap})

    out_items: list[dict[str, Any]] = []
    succeeded = 0
    failed = 0
    skipped_cap = 0

    for entry in items:
        if succeeded >= cap:
            log.info("daily cap reached, skipping rest", extra={"cap": cap})
            skipped_cap += 1
            out_items.append({**entry, "skipped_reason": "daily_cap"})
            continue

        video_id = entry["video_id"]
        platform = entry["platform"]
        video_dir = out_root / video_id
        video_dir.mkdir(parents=True, exist_ok=True)
        result_path = video_dir / "result.json"

        # Idempotency: skip if we've already produced a passing result.
        existing = _read_json(result_path)
        if existing and existing.get("qc_passed"):
            log.info("skipping (already generated + QC passed)", extra={"video_id": video_id})
            out_items.append({"video_id": video_id, "platform": platform, "qc_passed": True,
                              "path": f"{video_id}/result.json", "reused": True})
            succeeded += 1
            continue

        prompt_doc = _read_json(PROMPTS_ROOT / account.handle / today_str() / entry["path"])
        if not prompt_doc:
            log.error("missing prompt doc", extra={"video_id": video_id, "path": entry.get("path")})
            failed += 1
            continue

        try:
            if platform == "arcads":
                gen_result = _generate_arcads(prompt_doc, account, video_dir, log)
            elif platform == "higgsfield":
                gen_result = _generate_higgsfield(prompt_doc, account, video_dir, log)
            else:
                raise RuntimeError(f"unknown platform: {platform}")

            result = {
                "video_id": video_id,
                "account": account.handle,
                "platform": platform,
                "generated_at": datetime.now(tz=timezone.utc).isoformat(),
                "files": gen_result["files"],
                "qc_passed": gen_result["qc_passed"],
                "duration_seconds_total": gen_result["duration_seconds_total"],
                "metadata": prompt_doc.get("metadata", {}),
                "evidence_screenshot_required": prompt_doc.get("evidence_screenshot_required", False),
                "evidence_payload": prompt_doc.get("evidence_payload"),
            }
            _write_json(result_path, result)

            out_items.append({
                "video_id": video_id,
                "platform": platform,
                "qc_passed": result["qc_passed"],
                "path": f"{video_id}/result.json",
            })
            if result["qc_passed"]:
                succeeded += 1
            else:
                failed += 1
            log.info(
                "video produced",
                extra={"video_id": video_id, "qc_passed": result["qc_passed"],
                       "duration_s": result["duration_seconds_total"]},
            )

        except Exception as e:
            log.exception("video generation failed", extra={"video_id": video_id, "err": str(e)})
            _write_json(result_path, {
                "video_id": video_id,
                "account": account.handle,
                "platform": platform,
                "generated_at": datetime.now(tz=timezone.utc).isoformat(),
                "qc_passed": False,
                "error": str(e),
                "metadata": prompt_doc.get("metadata", {}),
            })
            out_items.append({
                "video_id": video_id, "platform": platform, "qc_passed": False,
                "path": f"{video_id}/result.json", "error": str(e),
            })
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
    log.info(
        "videogen complete",
        extra={"succeeded": succeeded, "failed": failed, "skipped_cap": skipped_cap},
    )
    return manifest_out
