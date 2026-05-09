"""FFmpeg composition primitives + final encoder + Discord-style evidence
screenshot renderer.

Primitives are intentionally small and chainable. Each takes input paths and
produces an output file. The orchestrator (editor.py) calls them in order and
keeps each intermediate so failures are debuggable.

Final-encode targets: 1080x1920 (9:16), h264 (CRF 21, fast preset), AAC
192k stereo, 30 fps, +faststart for fast TikTok upload.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from core.logger import get_logger

FONTS_DIR = Path("data/assets/fonts")
ASSETS_DIR = Path("data/assets")


class FormatterError(RuntimeError):
    pass


def _ffmpeg(args: list[str]) -> None:
    proc = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", *args],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise FormatterError(f"ffmpeg failed: {proc.stderr.strip() or 'unknown error'}")


def _have(binary: str) -> bool:
    return shutil.which(binary) is not None


# --- Composition primitives ---------------------------------------------------

def trim(in_path: Path, out_path: Path, *, duration: float) -> Path:
    """Hard-trim a clip to `duration` seconds. Re-encodes (no codec copy)
    because some inputs have keyframes that don't align cleanly.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    _ffmpeg([
        "-i", str(in_path),
        "-t", f"{duration:.3f}",
        "-c:v", "libx264", "-preset", "fast", "-crf", "21",
        "-c:a", "aac", "-b:a", "128k",
        "-pix_fmt", "yuv420p",
        str(out_path),
    ])
    return out_path


def concat_clips(clips: list[Path], out_path: Path) -> Path:
    """Concat-demuxer concat. All clips must share codec/format/resolution —
    `trim` above produces compatible inputs.
    """
    if not clips:
        raise FormatterError("concat_clips called with no inputs")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    list_file = out_path.with_suffix(".concat.txt")
    list_file.write_text(
        "".join(f"file '{p.resolve()}'\n" for p in clips),
        encoding="utf-8",
    )
    _ffmpeg([
        "-f", "concat", "-safe", "0", "-i", str(list_file),
        "-c", "copy",
        str(out_path),
    ])
    list_file.unlink(missing_ok=True)
    return out_path


def burn_subtitles(
    in_path: Path,
    ass_path: Path,
    out_path: Path,
    *,
    fonts_dir: Path = FONTS_DIR,
) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # subtitles filter requires escaping on the path.
    ass_for_filter = str(ass_path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    fonts_arg = ""
    if fonts_dir.is_dir():
        fonts_for_filter = str(fonts_dir).replace("\\", "\\\\").replace(":", "\\:")
        fonts_arg = f":fontsdir={fonts_for_filter}"
    _ffmpeg([
        "-i", str(in_path),
        "-vf", f"subtitles='{ass_for_filter}'{fonts_arg}",
        "-c:v", "libx264", "-preset", "fast", "-crf", "21",
        "-c:a", "copy",
        "-pix_fmt", "yuv420p",
        str(out_path),
    ])
    return out_path


def overlay_image(
    video_in: Path,
    image_in: Path,
    out_path: Path,
    *,
    start_seconds: float,
    end_seconds: float,
    position: str = "center",
) -> Path:
    """Composite a still image onto the video between `start_seconds` and
    `end_seconds`. `position`: 'center' or 'lower_third'.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if position == "lower_third":
        xy = "x=(W-w)/2:y=H*0.66-h/2"
    else:
        xy = "x=(W-w)/2:y=(H-h)/2"
    enable = f"between(t,{start_seconds:.3f},{end_seconds:.3f})"
    _ffmpeg([
        "-i", str(video_in), "-i", str(image_in),
        "-filter_complex",
        f"[1:v]scale=900:-1[ov];[0:v][ov]overlay={xy}:enable='{enable}'",
        "-c:v", "libx264", "-preset", "fast", "-crf", "21",
        "-c:a", "copy",
        "-pix_fmt", "yuv420p",
        str(out_path),
    ])
    return out_path


# --- Evidence screenshot (passivepoly) ---------------------------------------

def render_evidence_screenshot(
    payload: dict[str, Any],
    out_path: Path,
    *,
    width: int = 900,
    height: int = 520,
) -> Path | None:
    """Renders a Discord-themed alert card for passivepoly. Falls back to
    PIL's default font if no font is bundled. Returns None if Pillow is not
    installed (the editor will skip the overlay step).
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        get_logger("editor.formatter").warning(
            "Pillow not installed — skipping evidence screenshot. "
            "`pip install Pillow` to enable."
        )
        return None

    out_path.parent.mkdir(parents=True, exist_ok=True)

    bg = (47, 49, 54, 255)        # Discord dark
    title_bar = (54, 57, 63, 255)
    accent = (88, 101, 242, 255)  # Discord blurple
    text_main = (240, 240, 245, 255)
    text_muted = (185, 187, 190, 255)

    img = Image.new("RGBA", (width, height), bg)
    draw = ImageDraw.Draw(img)
    draw.rectangle([(0, 0), (width, 64)], fill=title_bar)
    draw.rectangle([(0, 64), (8, height)], fill=accent)

    def _font(size: int) -> Any:
        for candidate in (
            FONTS_DIR / "Inter-Bold.otf",
            FONTS_DIR / "Inter-Regular.otf",
        ):
            if candidate.exists():
                try:
                    return ImageFont.truetype(str(candidate), size)
                except OSError:
                    pass
        return ImageFont.load_default()

    title_font = _font(24)
    headline_font = _font(34)
    field_font = _font(22)

    draw.text((24, 18), "PassivePoly  ·  Whale Alert", fill=text_main, font=title_font)

    headline = (payload.get("headline") or "").strip() or "(no headline)"
    draw.text((28, 88), headline, fill=text_main, font=headline_font)

    y = 150
    fields: dict[str, Any] = (payload.get("fields") or {})
    for key, value in fields.items():
        line = f"{key}: {value}"
        draw.text((28, y), line, fill=text_muted, font=field_font)
        y += 36
        if y > height - 40:
            break

    sid = payload.get("source_event_id")
    if sid:
        draw.text((28, height - 40), f"id: {sid}", fill=(120, 122, 126, 255), font=field_font)

    img.save(out_path, format="PNG")
    return out_path


# --- Final encode (the export) -----------------------------------------------

def to_tiktok_mp4(
    in_path: Path,
    out_path: Path,
    *,
    target_w: int = 1080,
    target_h: int = 1920,
    fps: int = 30,
) -> Path:
    """Final TikTok-ready encode. Center-crops to the target aspect ratio if
    the input isn't already 9:16, then scales to exactly target_w x target_h.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    aspect = target_w / target_h
    # Crop to target aspect (preserves height if too wide, preserves width if too tall),
    # then scale to exact size.
    vf = (
        f"crop='if(gt(a,{aspect}),ih*{aspect},iw)':'if(gt(a,{aspect}),ih,iw/{aspect})',"
        f"scale={target_w}:{target_h}:flags=lanczos,setsar=1"
    )
    _ffmpeg([
        "-i", str(in_path),
        "-vf", vf,
        "-r", str(fps),
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
        "-movflags", "+faststart",
        str(out_path),
    ])
    return out_path


# --- Sanity ------------------------------------------------------------------

def assert_ffmpeg_present() -> None:
    if not _have("ffmpeg") or not _have("ffprobe"):
        raise FormatterError("ffmpeg/ffprobe not on PATH — install ffmpeg")
