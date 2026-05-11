"""Agent 7: Publisher (orchestrator).

Per slot fire (publisher_1 at noon, publisher_2 at 6pm):

  1. Ask scheduler.pick_video(account, slot) for the next un-posted video.
  2. Build the final caption: script's caption + finalized hashtags.
  3. Build the link plan via affiliate_linker (TikTok Shop or PassivePoly).
  4. Upload + publish via TikTokPublishClient.
  5. Best-effort: post the affiliate/whop link as a pinned comment (the
     Content Posting API tier most users have doesn't expose this — we log
     the intent either way so a downstream bot or human can fulfill it).
  6. Write data/published_log/<handle>/<today>/<video_id>.json (audit trail).

Idempotency: scheduler.pick_video skips anything already in published_log.
A re-run at the same slot is a no-op once the post succeeded.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agents.music_scout.scout import log_music_use, pick_music_id_for_video
from agents.publisher import affiliate_linker, hashtag_gen
from agents.publisher.scheduler import PUBLISHED_LOG_ROOT, pick_video
from core.config_loader import AccountConfig
from core.dateutils import today_str
from core.logger import get_logger
from integrations.tiktok_publish_client import (
    TikTokAPIError,
    TikTokCommentsUnavailable,
    TikTokPublishClient,
)

FINAL_VIDEOS_ROOT = Path("data/final_videos")
SCRIPTS_ROOT = Path("data/scripts")


def _today_log_dir(handle: str) -> Path:
    d = PUBLISHED_LOG_ROOT / handle / today_str()
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


def _script_for(handle: str, video_id: str) -> dict[str, Any] | None:
    doc = _read_json(SCRIPTS_ROOT / handle / today_str() / "scripts.json")
    if not doc:
        return None
    for s in doc.get("scripts", []):
        if s.get("video_id") == video_id:
            return s
    return None


def _editor_result(handle: str, video_id: str) -> dict[str, Any] | None:
    return _read_json(FINAL_VIDEOS_ROOT / handle / today_str() / video_id / "result.json")


def _build_caption(
    script: dict[str, Any],
    hashtags: list[str],
    appendix: str | None,
) -> str:
    base = (script.get("caption") or "").strip()
    parts = [base] if base else []
    if appendix:
        parts.append(appendix.strip())
    if hashtags:
        parts.append(" ".join(hashtags))
    return "\n\n".join(p for p in parts if p)[:2200]


def _update_daily_manifest(handle: str, entry: dict[str, Any]) -> None:
    path = _today_log_dir(handle) / "manifest.json"
    doc = _read_json(path) or {"account": handle, "date": today_str(), "items": []}
    items = list(doc.get("items", []))
    # Keep the most recent entry per video_id.
    items = [it for it in items if it.get("video_id") != entry["video_id"]]
    items.append(entry)
    doc["items"] = items
    doc["updated_at"] = datetime.now(tz=timezone.utc).isoformat()
    _write_json(path, doc)


def run(account: AccountConfig, ctx: dict[str, Any]) -> dict[str, Any]:
    log = get_logger("publisher", account.handle)
    slot = ctx.get("slot", "publisher_1")
    log.info("publisher fire", extra={"slot": slot})

    chosen = pick_video(account, slot)
    if not chosen:
        log.info("nothing to publish (cap hit or empty queue)")
        return {"slot": slot, "skipped": True, "reason": "no_candidate"}

    video_id = chosen["video_id"]
    final_path = FINAL_VIDEOS_ROOT / account.handle / today_str() / chosen["final_path"]
    if not final_path.exists():
        log.error("final.mp4 missing", extra={"video_id": video_id, "path": str(final_path)})
        return {"slot": slot, "skipped": True, "reason": "missing_final_mp4"}

    script = _script_for(account.handle, video_id)
    if not script:
        log.error("script not found", extra={"video_id": video_id})
        return {"slot": slot, "skipped": True, "reason": "missing_script"}

    editor_doc = _editor_result(account.handle, video_id) or {}
    metadata = (editor_doc.get("metadata") or {})

    # 1. Hashtags.
    hashtags_final, hashtag_debug = hashtag_gen.finalize(
        account=account,
        script_hashtags=script.get("hashtags", []),
    )

    # 2. Link plan.
    try:
        plan = affiliate_linker.build_link_plan(
            account=account, script_metadata={**metadata, **script},
        )
    except affiliate_linker.AffiliateLinkerError as e:
        log.error("affiliate linker failed", extra={"err": str(e)})
        return {"slot": slot, "skipped": True, "reason": "linker_error", "error": str(e)}

    # 3. Caption.
    caption = _build_caption(script, hashtags_final, plan.in_caption_appendix)

    # 3b. Music ID from Agent 9's catalog. Attaching a music_id at upload
    # time is what gives the video TikTok's trending-sound boost — baking
    # the audio into the mp4 via ffmpeg does not.
    music_track = pick_music_id_for_video(account)
    music_id = (music_track or {}).get("music_id")
    if music_track:
        log.info(
            "music_id selected",
            extra={
                "music_id": music_id,
                "title": music_track.get("title"),
                "trending_score": music_track.get("trending_score"),
            },
        )
    else:
        log.info("no music_id available — TikTok will use the video's baked audio")

    # 4. Upload + publish.
    creds = account.raw.get("api_credentials") or {}
    client = TikTokPublishClient(access_token=creds.get("tiktok_session", ""))

    log_entry: dict[str, Any] = {
        "video_id": video_id,
        "account": account.handle,
        "slot": slot,
        "posted_at": None,
        "caption_final": caption,
        "hashtags_final": hashtags_final,
        "hashtag_debug": hashtag_debug,
        "link_plan": {
            "type": plan.type,
            "bio_url": plan.bio_url,
            "pinned_comment_text": plan.pinned_comment_text,
            "metadata": plan.metadata,
        },
        "music": {
            "music_id": music_id,
            "title": (music_track or {}).get("title"),
            "trending_score": (music_track or {}).get("trending_score"),
        } if music_track else None,
        "publish_id": None,
        "publish_status": None,
        "tiktok_post_ids": [],
        "comment": {"attempted": False, "succeeded": False, "error": None},
        "errors": [],
    }

    try:
        publish_result = client.post_video(
            file_path=final_path, caption=caption, music_id=music_id,
        )
        log_entry["publish_id"] = publish_result.get("publish_id")
        log_entry["publish_status"] = publish_result.get("status")
        log_entry["tiktok_post_ids"] = publish_result.get("post_ids", [])
        log_entry["posted_at"] = datetime.now(tz=timezone.utc).isoformat()
        log.info(
            "published",
            extra={
                "video_id": video_id,
                "publish_id": log_entry["publish_id"],
                "post_ids": log_entry["tiktok_post_ids"],
                "music_id": music_id,
            },
        )
        # Append to music_log.json so Agent 9's least-recently-used rotation
        # knows not to pick the same track again right away.
        if music_track:
            log_music_use(
                account_handle=account.handle,
                video_id=video_id,
                track=music_track,
            )
    except TikTokAPIError as e:
        log_entry["errors"].append(f"publish: {e}")
        log_entry["publish_status"] = "FAILED"
        log.error("publish failed", extra={"video_id": video_id, "err": str(e)})

    # 5. Best-effort pinned comment.
    if log_entry["publish_status"] in {"PUBLISH_COMPLETE", "PUBLISHED"} and plan.pinned_comment_text:
        post_ids = log_entry["tiktok_post_ids"]
        log_entry["comment"]["attempted"] = True
        if not post_ids:
            log_entry["comment"]["error"] = "no post_id returned"
        else:
            try:
                comment_id = client.post_comment(post_ids[0], plan.pinned_comment_text)
                pinned = client.pin_comment(post_ids[0], comment_id)
                log_entry["comment"]["succeeded"] = pinned
                log_entry["comment"]["comment_id"] = comment_id
            except TikTokCommentsUnavailable as e:
                log_entry["comment"]["error"] = f"unavailable: {e}"
                log.info(
                    "comment endpoint unavailable — bio link only",
                    extra={"video_id": video_id, "intended_text": plan.pinned_comment_text},
                )
            except Exception as e:
                log_entry["comment"]["error"] = str(e)
                log.warning("comment post failed", extra={"err": str(e)})

    # 6. Persist.
    log_path = _today_log_dir(account.handle) / f"{video_id}.json"
    _write_json(log_path, log_entry)
    _update_daily_manifest(account.handle, {
        "video_id": video_id,
        "slot": slot,
        "publish_status": log_entry["publish_status"],
        "tiktok_post_ids": log_entry["tiktok_post_ids"],
        "comment_succeeded": log_entry["comment"]["succeeded"],
    })

    return {"slot": slot, "video_id": video_id, "publish_status": log_entry["publish_status"]}
