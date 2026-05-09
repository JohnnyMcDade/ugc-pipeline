"""Prompt templates for the video prompt engineer (Agent 4).

Arcads payload formatting is fully deterministic (rule-based avatar/voice
selection + emotion/pacing derivation from hook_type) — no LLM call needed,
so there is no Arcads prompt here.

Higgsfield prompt expansion DOES use Claude. Given a script's `broll_concept`
plus the persona's location anchor and visual vocabulary, Claude produces:
  - one `hero_clip` prompt (the dominant establishing shot)
  - one prompt per `on_screen_overlay` segment (what plays UNDER each overlay)

Each prompt names the camera grammar, lighting, and style refs explicitly so
the Higgsfield API call is repeatable, not vibes-based.
"""

from __future__ import annotations

import json
from typing import Any


HIGGSFIELD_EXPANSION_SYSTEM = """You are a cinematic prompt engineer for \
Higgsfield AI. You convert a script's `broll_concept` (one sentence) into \
concrete, camera-aware Higgsfield prompts.

Hard rules:
- Anchor every prompt to the persona's `location_anchor`. If they're \
Scottsdale, Arizona, that means saguaro, red rock, Camelback, desert light — \
NOT generic "luxury" or "city skyline."
- Pull subjects/scenes from the persona's `visual_vocabulary` when possible. \
You can combine and remix entries, but stay inside this aesthetic — do not \
invent wildly different scenes.
- Specify camera grammar from the supplied list (one per clip). Be explicit: \
"slow dolly in on a Mac Studio displaying Polymarket markets" beats "shot of \
a computer."
- Specify lighting + time of day (golden hour, blue hour, harsh midday, \
neon dusk, etc.).
- Include the supplied style_refs verbatim somewhere in the prompt.
- Bias the visual based on the script's `category`:
    whale_alert      → trading UI in frame, urgency w/o panic
    luxury_lifestyle → no UI, environment is the story
    educational      → minimalist desktop or notebook, no clutter
    proof_results    → screens / dashboards visible (Agent 6 burns the real screenshot over)
- For passivepoly: the persona is a calm 19yo. Avoid 'rich young guy' clichés \
(no champagne sprays, no money-fan, no Lamborghini doors-up). Subtle flex only.

You produce ONE Higgsfield prompt for each on_screen_overlay segment AND a \
hero_clip prompt for the dominant shot. The hero_clip can be reused if there \
is only one overlay.

Output strict JSON:
{
  "hero_clip": {
    "prompt": "<full prompt with subject + action + location + lighting + style_refs>",
    "duration_seconds": 6,
    "camera": "<one phrase from camera_grammar>",
    "negative_prompt": "<echo the supplied negative_prompt, optionally extended>"
  },
  "segments": [
    {
      "overlay_index": 0,
      "overlay_text": "<echo the overlay text it plays under>",
      "prompt": "<full prompt>",
      "duration_seconds": 4,
      "camera": "<one phrase from camera_grammar>",
      "negative_prompt": "<...>"
    }
  ]
}

`segments` length MUST equal the number of on_screen_overlays in the input.
"""


def higgsfield_expansion_user_prompt(
    script: dict[str, Any],
    higgsfield_cfg: dict[str, Any],
    target_duration_seconds: int,
) -> str:
    return (
        f"TARGET TOTAL VIDEO DURATION: {target_duration_seconds}s\n"
        f"DEFAULT PER-CLIP DURATION (Higgsfield can do 5-10s): "
        f"{higgsfield_cfg.get('default_clip_duration_seconds', 6)}s\n\n"
        "PERSONA LOCATION + AESTHETIC:\n"
        + json.dumps(
            {
                "location_anchor": higgsfield_cfg.get("location_anchor"),
                "visual_vocabulary": higgsfield_cfg.get("visual_vocabulary", []),
                "style_refs": higgsfield_cfg.get("style_refs", []),
                "camera_grammar": higgsfield_cfg.get("camera_grammar", []),
                "category_visual_bias": higgsfield_cfg.get("category_visual_bias", {}),
                "negative_prompt": higgsfield_cfg.get("negative_prompt", ""),
            },
            indent=2,
        )
        + "\n\nSCRIPT (the thing you are visualizing for):\n"
        + json.dumps(
            {
                "category": script.get("category"),
                "broll_concept": script.get("broll_concept"),
                "voiceover_text": script.get("voiceover_text"),
                "on_screen_overlays": script.get("on_screen_overlays", []),
                "evidence_payload": script.get("evidence_payload"),
            },
            indent=2,
        )
        + "\n\nReturn JSON only. `segments` length must equal "
        f"{len(script.get('on_screen_overlays', []))}."
    )
