"""Caption generation for Agent 6.

Two sources, one output format:

  Arcads      → take the script's voiceover_text and distribute words evenly
                across the video duration. The first cue covers ~3s and uses
                the "Hook" style (bigger, punchier); subsequent cues use
                "Body" style.
  Higgsfield  → take the script's on_screen_overlays directly — they already
                carry (t, duration) timing. overlay_index 0 → Hook style,
                rest → Overlay style.

Output is an ASS subtitle file ready to be burned in by the FFmpeg
`subtitles=` filter (libass).

Whisper integration is intentionally NOT included. The script is the source of
truth for what's said; an alignment pass via faster-whisper is a future
enhancement (toggle `editor.use_whisper_alignment` in master.yaml when ready).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_WORD_RE = re.compile(r"\S+")
# "Hook" gets ~the first 3 seconds. Tunable per account if needed later.
_HOOK_DURATION_S = 3.0
# Group N words per cue for the body — 3 reads natural at 150wpm.
_WORDS_PER_BODY_CUE = 3


@dataclass
class CaptionCue:
    start: float
    end: float
    text: str
    style: str  # "Hook" | "Body" | "Overlay"


def _seconds_to_ass(t: float) -> str:
    """ASS timestamp format: H:MM:SS.cs (centiseconds)."""
    if t < 0:
        t = 0
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def cues_from_voiceover(
    voiceover_text: str,
    *,
    total_duration_seconds: float,
    hook_text: str | None = None,
) -> list[CaptionCue]:
    """Build cues for an Arcads-style video. Strategy:
      - cue 0: covers 0..min(HOOK_DURATION, total). Text = `hook_text` if
        supplied, else the first ~6 words of voiceover_text.
      - cues 1..N: distribute the REMAINING voiceover words evenly across
        the rest of the duration, in chunks of _WORDS_PER_BODY_CUE.
    """
    if not voiceover_text or total_duration_seconds <= 0:
        return []

    all_words = _WORD_RE.findall(voiceover_text)
    if not all_words:
        return []

    hook_end = min(_HOOK_DURATION_S, total_duration_seconds)
    hook_actual = hook_text.strip() if hook_text else " ".join(all_words[:6])

    # Decide how many words from voiceover are "consumed" by the hook so we
    # don't duplicate. If `hook_text` was supplied separately, body covers
    # the entire voiceover. Otherwise skip the first 6 words.
    body_words = all_words if hook_text else all_words[6:]

    cues: list[CaptionCue] = [CaptionCue(0.0, hook_end, hook_actual, "Hook")]

    if not body_words or hook_end >= total_duration_seconds:
        return cues

    body_duration = total_duration_seconds - hook_end
    chunks: list[list[str]] = [
        body_words[i : i + _WORDS_PER_BODY_CUE]
        for i in range(0, len(body_words), _WORDS_PER_BODY_CUE)
    ]
    if not chunks:
        return cues
    per_chunk = body_duration / len(chunks)
    t = hook_end
    for chunk in chunks:
        cues.append(CaptionCue(t, t + per_chunk, " ".join(chunk), "Body"))
        t += per_chunk
    # Pin the last cue's end to the exact total duration to avoid drift.
    if cues:
        cues[-1].end = total_duration_seconds
    return cues


def cues_from_overlays(overlays: list[dict[str, Any]]) -> list[CaptionCue]:
    """Build cues for a Higgsfield-style video — overlays already have
    (t, duration). Index 0 uses Hook style, rest use Overlay.
    """
    out: list[CaptionCue] = []
    for i, ov in enumerate(overlays or []):
        text = (ov.get("text") or "").strip()
        if not text:
            continue
        start = float(ov.get("t", 0.0))
        duration = float(ov.get("duration", 2.0))
        style = "Hook" if i == 0 else "Overlay"
        out.append(CaptionCue(start, start + duration, text, style))
    return out


def write_ass(
    cues: list[CaptionCue],
    out_path: Path,
    *,
    play_res_x: int = 1080,
    play_res_y: int = 1920,
    font_name: str = "Inter",
    body_size: int = 72,
    hook_size: int = 110,
) -> Path:
    """Write an ASS file with three styles (Hook / Body / Overlay) and the
    supplied cues. Colors are TikTok-default white text, black outline.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {play_res_x}
PlayResY: {play_res_y}
ScaledBorderAndShadow: yes
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Hook,{font_name},{hook_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,5,2,5,80,80,0,1
Style: Body,{font_name},{body_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,4,1,2,60,60,260,1
Style: Overlay,{font_name},{body_size + 8},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,4,1,5,60,60,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    lines: list[str] = [header]
    for c in cues:
        # ASS Text is the LAST field on the Dialogue line — embedded commas
        # are literal, not delimiters. Only newlines need escaping (\N).
        # Curly braces would start an inline override; brace them out.
        text = (
            c.text
            .replace("\n", "\\N")
            .replace("{", "\\{")
            .replace("}", "\\}")
        )
        lines.append(
            f"Dialogue: 0,{_seconds_to_ass(c.start)},{_seconds_to_ass(c.end)},"
            f"{c.style},,0,0,0,,{text}\n"
        )

    out_path.write_text("".join(lines), encoding="utf-8")
    return out_path
