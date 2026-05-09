"""Basic post-download QC.

Uses `ffprobe` (no extra Python deps) to inspect the downloaded mp4. Checks:

  1. File exists and is non-trivially large.
  2. ffprobe parses it (i.e. not a corrupted half-download).
  3. Duration is within tolerance of the expected target.
  4. Resolution is portrait (TikTok-shaped).
  5. Has at least one audio track when `expect_audio=True` (Arcads).
  6. The first half-second isn't completely black (catches the common "white
     screen → fade in" failures that some avatar generators produce).

Returns a dict — never raises on quality failure. Generator decides what to do.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

_MIN_FILE_BYTES = 200_000          # ~200 KB; smaller than this is almost certainly broken
_DURATION_TOLERANCE_S = 4.0
_BLACK_DETECT_THRESHOLD = 0.05     # fraction of luminance below which the frame is "black"


class QualityCheckError(RuntimeError):
    pass


def _have(binary: str) -> bool:
    return shutil.which(binary) is not None


def _ffprobe_json(path: Path) -> dict[str, Any]:
    if not _have("ffprobe"):
        raise QualityCheckError("ffprobe not found on PATH — install ffmpeg")
    out = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_format", "-show_streams",
            "-of", "json", str(path),
        ],
        capture_output=True, text=True, timeout=30,
    )
    if out.returncode != 0:
        raise QualityCheckError(f"ffprobe failed: {out.stderr.strip()}")
    return json.loads(out.stdout or "{}")


def _has_visible_first_frame(path: Path) -> bool:
    """Returns False if the first 0.5s is fully black (typical generator failure)."""
    if not _have("ffmpeg"):
        return True  # don't fail QC just because ffmpeg isn't there
    out = subprocess.run(
        [
            "ffmpeg", "-v", "error",
            "-i", str(path),
            "-vf", f"blackdetect=d=0.5:pix_th={_BLACK_DETECT_THRESHOLD}",
            "-an", "-f", "null", "-",
        ],
        capture_output=True, text=True, timeout=30,
    )
    # blackdetect writes to stderr only when it detects a black segment.
    return "black_start:0" not in out.stderr


def check_video(
    path: Path,
    *,
    expected_duration_seconds: float | None,
    expect_audio: bool,
    duration_tolerance_seconds: float = _DURATION_TOLERANCE_S,
) -> dict[str, Any]:
    issues: list[str] = []
    info: dict[str, Any] = {
        "path": str(path),
        "exists": path.exists(),
        "bytes": path.stat().st_size if path.exists() else 0,
    }

    if not info["exists"]:
        return {"passed": False, "issues": ["file does not exist"], "info": info}
    if info["bytes"] < _MIN_FILE_BYTES:
        issues.append(f"file too small ({info['bytes']} bytes)")
        return {"passed": False, "issues": issues, "info": info}

    try:
        probe = _ffprobe_json(path)
    except QualityCheckError as e:
        return {"passed": False, "issues": [f"ffprobe: {e}"], "info": info}

    streams = probe.get("streams", [])
    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]

    if not video_streams:
        issues.append("no video stream")
    else:
        v = video_streams[0]
        width = int(v.get("width", 0))
        height = int(v.get("height", 0))
        info["resolution"] = f"{width}x{height}"
        info["video_codec"] = v.get("codec_name")
        if width == 0 or height == 0:
            issues.append("missing resolution")
        elif height <= width:
            issues.append(f"not portrait ({width}x{height})")

    if expect_audio and not audio_streams:
        issues.append("no audio stream (expected — this is Arcads avatar output)")
    if audio_streams:
        info["audio_codec"] = audio_streams[0].get("codec_name")

    duration_s = float(probe.get("format", {}).get("duration", 0.0))
    info["duration_s"] = round(duration_s, 2)
    if expected_duration_seconds is not None and duration_s > 0:
        if abs(duration_s - expected_duration_seconds) > duration_tolerance_seconds:
            issues.append(
                f"duration {duration_s:.1f}s outside ±{duration_tolerance_seconds}s "
                f"of target {expected_duration_seconds}s"
            )

    if not _has_visible_first_frame(path):
        issues.append("first 0.5s is fully black")

    return {"passed": not issues, "issues": issues, "info": info}
