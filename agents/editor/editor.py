"""Agent 6: Editor — assembles the final publish-ready MP4.

For each video produced by Agent 5:

  Arcads path:
    1. Generate ASS captions from script.voiceover_text (Hook style + Body cues).
    2. Burn captions into arcads.mp4.
    3. Mix in background music (voice ducked at -18dB).
    4. Final-encode to 1080x1920 / h264 / aac / 30fps.

  Higgsfield path:
    1. Build per-overlay clip list (segment_i.mp4 if QC passed, else hero_clip.mp4),
       trim each to overlay duration, concat into one timeline.
    2. If `evidence_screenshot_required`: render Discord-themed PNG card from
       evidence_payload, composite onto the timeline at the evidence overlay's
       start..end window.
    3. Generate ASS from on_screen_overlays (Hook on overlay 0, Overlay style
       elsewhere). Burn into the timeline.
    4. Mix in background music (no voice — music is primary).
    5. Final-encode.

Output:
  data/final_videos/<handle>/<date>/<video_id>/final.mp4
  data/final_videos/<handle>/<date>/<video_id>/result.json
  data/final_videos/<handle>/<date>/manifest.json

Idempotent — if `final.mp4` already exists for a video_id today, skipped.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agents.editor import captions, formatter, music_mixer
from core.config_loader import AccountConfig, load_master
from core.dateutils import today_str
from core.logger import get_logger

RAW_VIDEOS_ROOT = Path("data/raw_videos")
FINAL_VIDEOS_ROOT = Path("data/final_videos")
SCRIPTS_ROOT = Path("data/scripts")


def _today_dir(handle: str) -> Path:
    d = FINAL_VIDEOS_ROOT / handle / today_str()
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


def _editor_cfg() -> dict[str, Any]:
    return load_master(Path("config/master.yaml")).editor


def _script_for(handle: str, video_id: str) -> dict[str, Any] | None:
    doc = _read_json(SCRIPTS_ROOT / handle / today_str() / "scripts.json")
    if not doc:
        return None
    for s in doc.get("scripts", []):
        if s.get("video_id") == video_id:
            return s
    return None


def _select_clip_per_overlay(
    files: list[dict[str, Any]],
    overlay_count: int,
    raw_video_dir: Path,
) -> list[Path]:
    """For Higgsfield videos. For each overlay i (0..N-1), return:
      - segment_i.mp4 if it exists & QC passed
      - else hero_clip.mp4 as fallback
    Raises if neither is available.
    """
    by_role: dict[tuple[str, int | None], Path] = {}
    hero_path: Path | None = None
    for f in files:
        if f.get("error"):
            continue
        if not (f.get("qc") or {}).get("passed"):
            continue
        path = raw_video_dir / f["name"]
        if not path.exists():
            continue
        if f["role"] == "hero":
            hero_path = path
        elif f["role"] == "segment":
            by_role[("segment", f.get("overlay_index"))] = path

    out: list[Path] = []
    for i in range(overlay_count):
        seg = by_role.get(("segment", i))
        chosen = seg or hero_path
        if not chosen:
            raise RuntimeError(f"no clip available for overlay {i} (no segment, no hero)")
        out.append(chosen)
    return out


def _assemble_arcads(
    *,
    video_id: str,
    raw_result: dict[str, Any],
    script: dict[str, Any],
    account: AccountConfig,
    work_dir: Path,
    out_final: Path,
    log,
) -> dict[str, Any]:
    raw_video_dir = RAW_VIDEOS_ROOT / account.handle / today_str() / video_id
    src = raw_video_dir / "arcads.mp4"
    if not src.exists():
        raise RuntimeError(f"arcads.mp4 missing at {src}")

    duration = float(raw_result.get("duration_seconds_total") or script.get("target_duration_seconds") or 30)

    # 1. Captions
    cues = captions.cues_from_voiceover(
        script["voiceover_text"],
        total_duration_seconds=duration,
        hook_text=script.get("hook"),
    )
    ass_path = work_dir / "captions.ass"
    captions.write_ass(
        cues,
        ass_path,
        font_name=_editor_cfg().get("caption_font", "Inter"),
        body_size=int(_editor_cfg().get("caption_size", 72)),
    )

    # 2. Burn
    burned = formatter.burn_subtitles(src, ass_path, work_dir / "01_captioned.mp4")

    # 3. Music
    editor_cfg_acct = (account.raw.get("editor") or {})
    music_subdir = editor_cfg_acct.get("music_subdir") or account.niche
    music_path = music_mixer.select_music(music_subdir, int(script.get("variant_index", 0)))
    log.info("music selection", extra={"subdir": music_subdir, "picked": str(music_path) if music_path else None})

    mixed = music_mixer.mix(
        video_in=burned,
        music_in=music_path,
        out_path=work_dir / "02_mixed.mp4",
        has_voice=True,
        music_volume_db=float(_editor_cfg().get("music_volume_db", -18)),
    )

    # 4. Final encode (defensive 9:16 + codec normalization)
    formatter.to_tiktok_mp4(mixed, out_final)

    return {
        "ass_path": str(ass_path.relative_to(work_dir.parent)),
        "music_used": str(music_path) if music_path else None,
        "duration_seconds": duration,
        "caption_cues": len(cues),
    }


def _assemble_higgsfield(
    *,
    video_id: str,
    raw_result: dict[str, Any],
    script: dict[str, Any],
    account: AccountConfig,
    work_dir: Path,
    out_final: Path,
    log,
) -> dict[str, Any]:
    raw_video_dir = RAW_VIDEOS_ROOT / account.handle / today_str() / video_id
    overlays = script.get("on_screen_overlays") or []
    if not overlays:
        raise RuntimeError("higgsfield script has no on_screen_overlays — cannot assemble timeline")

    # 1. Build the timeline by trimming each chosen clip to its overlay duration.
    chosen_clips = _select_clip_per_overlay(raw_result.get("files", []), len(overlays), raw_video_dir)
    trimmed_clips: list[Path] = []
    timeline_dir = work_dir / "trimmed"
    timeline_dir.mkdir(parents=True, exist_ok=True)
    for i, (clip, ov) in enumerate(zip(chosen_clips, overlays)):
        out = timeline_dir / f"clip_{i}.mp4"
        formatter.trim(clip, out, duration=float(ov.get("duration", 4.0)))
        trimmed_clips.append(out)

    # The trimmed clips were re-encoded together, so concat-demuxer is safe.
    concat_path = work_dir / "01_concat.mp4"
    formatter.concat_clips(trimmed_clips, concat_path)

    # 2. Evidence screenshot composite (if required and renderable).
    after_screenshot = concat_path
    if raw_result.get("evidence_screenshot_required"):
        evidence = raw_result.get("evidence_payload") or script.get("evidence_payload") or {}
        png_path = work_dir / "evidence.png"
        rendered = formatter.render_evidence_screenshot(evidence, png_path)
        if rendered:
            # Show the screenshot during the "evidence" overlay window — that's
            # whichever overlay isn't index 0 (hook) and isn't the CTA. Heuristic:
            # the second overlay if there are 3+, else overlay index 1.
            evidence_idx = 1 if len(overlays) >= 2 else 0
            ev = overlays[evidence_idx]
            after_screenshot = formatter.overlay_image(
                concat_path, rendered,
                work_dir / "02_with_evidence.mp4",
                start_seconds=float(ev.get("t", 3.0)),
                end_seconds=float(ev.get("t", 3.0)) + float(ev.get("duration", 5.0)),
                position="center",
            )
        else:
            log.warning("evidence requested but Pillow unavailable — skipping screenshot")

    # 3. Captions from overlays.
    cues = captions.cues_from_overlays(overlays)
    ass_path = work_dir / "captions.ass"
    captions.write_ass(
        cues,
        ass_path,
        font_name=_editor_cfg().get("caption_font", "Inter"),
        body_size=int(_editor_cfg().get("caption_size", 72)),
    )
    burned = formatter.burn_subtitles(after_screenshot, ass_path, work_dir / "03_captioned.mp4")

    # 4. Music — passivepoly is silent, so music is primary audio.
    editor_cfg_acct = (account.raw.get("editor") or {})
    music_subdir = editor_cfg_acct.get("music_subdir") or account.niche
    music_path = music_mixer.select_music(music_subdir, int(script.get("variant_index", 0)))
    log.info("music selection", extra={"subdir": music_subdir, "picked": str(music_path) if music_path else None})

    mixed = music_mixer.mix(
        video_in=burned,
        music_in=music_path,
        out_path=work_dir / "04_mixed.mp4",
        has_voice=False,
        music_volume_db=float(_editor_cfg().get("music_volume_db", -10)),
    )

    # 5. Final encode.
    formatter.to_tiktok_mp4(mixed, out_final)

    return {
        "ass_path": str(ass_path.relative_to(work_dir.parent)),
        "music_used": str(music_path) if music_path else None,
        "evidence_rendered": raw_result.get("evidence_screenshot_required") and (work_dir / "evidence.png").exists(),
        "overlay_count": len(overlays),
        "caption_cues": len(cues),
    }


def run(account: AccountConfig, ctx: dict[str, Any]) -> dict[str, Any]:
    log = get_logger("editor", account.handle)
    out_root = _today_dir(account.handle)

    formatter.assert_ffmpeg_present()

    raw_manifest = _read_json(RAW_VIDEOS_ROOT / account.handle / today_str() / "manifest.json")
    if not raw_manifest:
        log.warning("no raw_videos manifest for today")
        m = {"account": account.handle, "items": [], "warning": "no_raw_manifest"}
        _write_json(out_root / "manifest.json", m)
        return m

    items_in = raw_manifest.get("items", [])
    log.info("editor start", extra={"queued": len(items_in)})

    items_out: list[dict[str, Any]] = []
    succeeded = 0
    failed = 0
    skipped_qc = 0

    for entry in items_in:
        video_id = entry["video_id"]
        platform = entry["platform"]

        # Arcads has only one file — if its QC failed there's no fallback,
        # skip is correct. Higgsfield has hero_clip + N segments, and
        # _select_clip_per_overlay falls back to hero when a segment fails,
        # so a single bad clip shouldn't kill the whole video. Try assembly
        # and let it raise naturally if there are no usable clips at all.
        if platform == "arcads" and not entry.get("qc_passed"):
            log.info("skipping (Agent 5 QC failed)", extra={"video_id": video_id})
            skipped_qc += 1
            continue

        video_out_dir = out_root / video_id
        video_out_dir.mkdir(parents=True, exist_ok=True)
        out_final = video_out_dir / "final.mp4"
        result_path = video_out_dir / "result.json"

        if out_final.exists():
            log.info("skipping (final.mp4 already exists)", extra={"video_id": video_id})
            existing = _read_json(result_path) or {"video_id": video_id, "final_path": str(out_final.relative_to(out_root))}
            items_out.append({**existing, "reused": True})
            succeeded += 1
            continue

        raw_result = _read_json(RAW_VIDEOS_ROOT / account.handle / today_str() / video_id / "result.json")
        if not raw_result:
            log.error("missing raw result.json", extra={"video_id": video_id})
            failed += 1
            continue
        script = _script_for(account.handle, video_id)
        if not script:
            log.error("script not found for video_id", extra={"video_id": video_id})
            failed += 1
            continue

        work_dir = video_out_dir / "work"
        work_dir.mkdir(parents=True, exist_ok=True)

        try:
            if platform == "arcads":
                detail = _assemble_arcads(
                    video_id=video_id, raw_result=raw_result, script=script,
                    account=account, work_dir=work_dir, out_final=out_final, log=log,
                )
            elif platform == "higgsfield":
                detail = _assemble_higgsfield(
                    video_id=video_id, raw_result=raw_result, script=script,
                    account=account, work_dir=work_dir, out_final=out_final, log=log,
                )
            else:
                raise RuntimeError(f"unknown platform: {platform}")

            result = {
                "video_id": video_id,
                "account": account.handle,
                "platform": platform,
                "generated_at": datetime.now(tz=timezone.utc).isoformat(),
                "final_path": str(out_final.relative_to(out_root)),
                "metadata": {
                    **(raw_result.get("metadata") or {}),
                    "caption": script.get("caption"),
                    "hashtags": script.get("hashtags", []),
                    "cta_url": script.get("cta_url"),
                },
                "detail": detail,
            }
            _write_json(result_path, result)
            items_out.append({
                "video_id": video_id, "platform": platform,
                "final_path": result["final_path"],
            })
            succeeded += 1
            log.info("video assembled", extra={"video_id": video_id, "final": str(out_final)})

        except Exception as e:
            log.exception("editor failed", extra={"video_id": video_id, "err": str(e)})
            _write_json(result_path, {
                "video_id": video_id,
                "account": account.handle,
                "platform": platform,
                "generated_at": datetime.now(tz=timezone.utc).isoformat(),
                "error": str(e),
            })
            failed += 1

    manifest = {
        "account": account.handle,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "items": items_out,
        "succeeded": succeeded,
        "failed": failed,
        "skipped_qc": skipped_qc,
    }
    _write_json(out_root / "manifest.json", manifest)
    log.info(
        "editor complete",
        extra={"succeeded": succeeded, "failed": failed, "skipped_qc": skipped_qc},
    )
    return manifest
