"""Prompt templates for the hook researcher (Agent 2).

Two prompts:
  1. HOOK_IDENTIFICATION  — given a video's caption + first-line transcript,
     extract the literal opening hook (what the viewer hears in 0-3s).
  2. HOOK_PATTERN_CLUSTER — given a list of hooks + their engagement metrics
     + the account persona, cluster them into reusable templates.

The system blocks are persona-free so they cache across all three accounts.
Persona, banned phrases, and yesterday's winners are injected via the user
prompt — that's the part that varies per account.
"""

from __future__ import annotations

import json
from typing import Any


HOOK_IDENTIFICATION_SYSTEM = """You extract the LITERAL OPENING HOOK from a \
TikTok video. The hook is what the viewer reads or hears in the first 0-3 \
seconds — usually the on-screen text or the first sentence of the voiceover.

Rules:
- Return the exact text, do not paraphrase or "improve" it.
- If the on-screen text and the voiceover differ, prefer on-screen text \
(that's what stops the scroll).
- If neither is available, fall back to the first clause of the caption.
- Strip emoji, hashtags, and trailing ellipses.

Output strict JSON:
{
  "videos": [
    { "video_id": "<echoed>", "hook": "<verbatim opening>", "hook_type": "POV|contrarian|curiosity|social_proof|identity|numerical|question|other", "confidence": 0.0 }
  ]
}
"""


def hook_identification_user_prompt(videos: list[dict[str, Any]]) -> str:
    """`videos` items: {video_id, caption, first_line_transcript?, on_screen_text?}."""
    return (
        "Identify the opening hook for each video. Return JSON only.\n\n"
        + json.dumps(videos, indent=2)
    )


HOOK_PATTERN_CLUSTER_SYSTEM = """You analyze TikTok hooks for a specific \
creator persona and cluster them into reusable PATTERNS that can be used to \
write new scripts.

A pattern is a template with one or more variable slots, e.g.:
  template:  "POV: <reaction> when I {action}"
  examples:  ["POV: she smelled my neck and asked what I was wearing", ...]

Your job:
1. Cluster the input hooks into 5-12 distinct patterns. Group by structural \
similarity (POV vs contrarian vs identity-callout etc.), NOT by topic.
2. For each pattern, write a compact template with bracketed slots.
3. Score each pattern 0.0-1.0 on persona-fit. The persona is given. \
Persona-fit means: would this creator actually open a video this way without \
sounding off?
4. Reject any pattern that requires a banned phrase. Mark `banned_phrase_clean: false`.
5. If yesterday's winners are provided, weight their patterns higher (the \
caller will use this signal to upweight your `claude_score`).

Output strict JSON:
{
  "patterns": [
    {
      "id": "<short_snake_case_id>",
      "category": "POV|contrarian|curiosity|social_proof|identity|numerical|question|other",
      "template": "<template with <bracketed> slots>",
      "examples": ["<verbatim example 1>", "<verbatim example 2>"],
      "source_video_ids": ["<id1>", "<id2>"],
      "claude_score": 0.0,
      "persona_fit_notes": "<one sentence>",
      "banned_phrase_clean": true,
      "echoes_yesterday_winner": false
    }
  ]
}
"""


def hook_pattern_cluster_user_prompt(
    persona: dict[str, Any],
    banned_phrases: list[str],
    hooks: list[dict[str, Any]],
    yesterday_winners: list[dict[str, Any]] | None,
) -> str:
    """`hooks` items: {video_id, hook, hook_type, view_count, like_count,
    comment_count, share_count, engagement_rate}."""
    parts = [
        "PERSONA:\n" + json.dumps(persona, indent=2),
        "BANNED PHRASES (reject any pattern that requires these):\n"
        + json.dumps(banned_phrases, indent=2),
        "INPUT HOOKS (with engagement metrics):\n" + json.dumps(hooks, indent=2),
    ]
    if yesterday_winners:
        parts.append(
            "YESTERDAY'S WINNERS (videos this account posted that overperformed — "
            "their patterns deserve a higher claude_score):\n"
            + json.dumps(yesterday_winners, indent=2)
        )
    parts.append("Return JSON only.")
    return "\n\n".join(parts)
