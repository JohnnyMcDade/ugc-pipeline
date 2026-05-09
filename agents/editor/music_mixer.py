"""Music selection + audio mixing.

Music library convention:
  data/music/<subdir>/*.mp3        # subdir per account, defined in YAML
  data/music/<subdir>/*.m4a
  data/music/<subdir>/*.wav

Selection: deterministic rotation by `variant_index` so re-runs pick the same
file for the same script. If the directory is missing or empty, returns None
and the editor proceeds without music (logged as a warning, not a failure).

Mixing strategies:
  has_voice=True   → static-duck: voice at 0dB, music at -18dB, amix
  has_voice=False  → music is the primary audio at -10dB (Higgsfield clips
                     are silent, so music carries the audio)

We use static ducking (constant volume) instead of sidechaincompress because
TikTok already loudness-normalizes uploads, and static ducking is one fewer
filter graph to debug.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from core.logger import get_logger

MUSIC_ROOT = Path("data/music")
_AUDIO_EXTS = {".mp3", ".m4a", ".wav", ".aac"}


class MusicMixerError(RuntimeError):
    pass


def select_music(subdir: str | None, variant_index: int) -> Path | None:
    """Returns a music file path, or None if none available."""
    if not subdir:
        return None
    dir_path = MUSIC_ROOT / subdir
    if not dir_path.is_dir():
        return None
    files = sorted(p for p in dir_path.iterdir() if p.suffix.lower() in _AUDIO_EXTS)
    if not files:
        return None
    return files[variant_index % len(files)]


def _ffmpeg(args: list[str]) -> None:
    """Run an ffmpeg command, raise with stderr on non-zero exit."""
    proc = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", *args],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise MusicMixerError(f"ffmpeg failed: {proc.stderr.strip()}")


def mix(
    *,
    video_in: Path,
    music_in: Path | None,
    out_path: Path,
    has_voice: bool,
    voice_volume_db: float = 0.0,
    music_volume_db: float = -18.0,
) -> Path:
    """Produces `out_path` from `video_in` with optional `music_in` mixed in.

    If `music_in` is None, this is a copy operation that re-encodes audio to
    AAC and ensures a single audio track exists (some Higgsfield clips have
    no audio at all — TikTok rejects videos with zero audio streams).
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    log = get_logger("editor.music_mixer")

    if music_in is None:
        # Ensure an audio track exists. If the input has none, synthesize silence.
        log.info("no music — ensuring single silent track if needed",
                 extra={"video": str(video_in)})
        _ffmpeg([
            "-i", str(video_in),
            "-f", "lavfi", "-t", "60", "-i", "anullsrc=r=44100:cl=stereo",
            "-map", "0:v",
            # Try input audio first; if missing, fall back to anullsrc on 1.
            "-map", "0:a?", "-map", "1:a",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-shortest",
            str(out_path),
        ])
        return out_path

    if has_voice:
        # Voice (input 0 audio) at 0dB + music (input 1 audio) at -18dB,
        # mixed and trimmed to the video's duration.
        filter_complex = (
            f"[0:a]volume={voice_volume_db}dB[v];"
            f"[1:a]volume={music_volume_db}dB,aloop=loop=-1:size=2e9[m];"
            f"[v][m]amix=inputs=2:duration=first:dropout_transition=2[a]"
        )
    else:
        # No voice — music carries the audio. Slightly louder than ducked.
        filter_complex = (
            f"[1:a]volume={max(music_volume_db, -10.0)}dB,"
            f"aloop=loop=-1:size=2e9[a]"
        )

    log.info("mixing music", extra={"music": str(music_in), "has_voice": has_voice})
    _ffmpeg([
        "-i", str(video_in), "-i", str(music_in),
        "-filter_complex", filter_complex,
        "-map", "0:v", "-map", "[a]",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest",
        str(out_path),
    ])
    return out_path
