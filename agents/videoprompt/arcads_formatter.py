"""Deterministic Arcads.ai payload formatter (no LLM call).

Maps a validated script dict + the account's `videogen.arcads` config into a
ready-to-POST payload. Selection rules:

  Avatar:  rotated by `variant_index % len(pool)` so the four daily variants
           use different avatars and the rotation is stable across re-runs.
  Voice:   rotated by `variant_index % len(pool)`. Same idempotency property.
  Emotion: derived from the script's `hook` text + persona archetype using
           a static lookup on hook_type (when present), falling back to
           account `default_emotion`.
  Pacing:  derived from hook_type, same fallback.

Why deterministic? Re-running engineer.py for the same day must produce the
same payload — Agent 5 keys video files by `video_id`, and we don't want a
Claude reroll to invalidate already-generated clips.
"""

from __future__ import annotations

from typing import Any

from core.config_loader import AccountConfig

# Mapping from hook_type → (emotion, pacing). Derived once from the patterns
# Agent 2 produces. Falls back to account defaults when hook_type is missing
# or unrecognized.
_HOOK_TYPE_DELIVERY = {
    "POV":          ("playful_confident", "natural"),
    "contrarian":   ("assertive",         "fast"),
    "curiosity":    ("intrigued",         "slow"),
    "social_proof": ("warm",              "natural"),
    "identity":     ("punchy",            "fast"),
    "numerical":    ("instructive",       "natural"),
    "question":     ("inquisitive",       "natural"),
    "other":        (None,                None),
}


class ArcadsConfigError(RuntimeError):
    pass


def _arcads_cfg(account: AccountConfig) -> dict[str, Any]:
    cfg = (account.raw.get("videogen") or {}).get("arcads") or {}
    if not cfg.get("avatar_pool"):
        raise ArcadsConfigError(
            f"@{account.handle}: videogen.arcads.avatar_pool is empty. "
            "Add at least one avatar entry (id, gender, age_range, vibe, environment)."
        )
    if not cfg.get("voice_pool"):
        raise ArcadsConfigError(
            f"@{account.handle}: videogen.arcads.voice_pool is empty. "
            "Add at least one voice entry."
        )
    return cfg


def _select_by_index(pool: list[dict[str, Any]], variant_index: int) -> dict[str, Any]:
    return pool[variant_index % len(pool)]


def _derive_delivery(
    script: dict[str, Any],
    arcads_cfg: dict[str, Any],
) -> tuple[str, str]:
    """Returns (emotion, pacing). Looks up hook_type → delivery, falls back
    to account defaults from arcads_cfg.
    """
    default_emotion = arcads_cfg.get("default_emotion", "confident")
    default_pacing = arcads_cfg.get("default_pacing", "natural")

    # The script doesn't explicitly carry hook_type, but the source pattern
    # does (Agent 2 stamps category on each pattern). When the writer keeps
    # provenance only as `source_pattern_id`, the engineer should look up the
    # category from the patterns file. We accept either via the optional
    # `pattern_category` field that engineer.py injects before calling us.
    hook_type = (script.get("pattern_category") or "other").lower()
    delivery = _HOOK_TYPE_DELIVERY.get(hook_type, (None, None))
    emotion = delivery[0] or default_emotion
    pacing = delivery[1] or default_pacing
    return emotion, pacing


def format_arcads_payload(
    script: dict[str, Any],
    account: AccountConfig,
) -> dict[str, Any]:
    """Returns the full Arcads-ready prompt object. Agent 5 will POST the
    `payload` field to the Arcads API verbatim.
    """
    cfg = _arcads_cfg(account)
    variant_index = int(script.get("variant_index", 0))

    avatar = _select_by_index(cfg["avatar_pool"], variant_index)
    voice = _select_by_index(cfg["voice_pool"], variant_index)
    emotion, pacing = _derive_delivery(script, cfg)

    payload = {
        "avatar_id": avatar["id"],
        "voice_id": voice["id"],
        "script_text": script["voiceover_text"],
        "emotion": emotion,
        "pacing": pacing,
        "aspect_ratio": cfg.get("aspect_ratio", "9:16"),
        "output_format": cfg.get("output_format", "mp4"),
    }

    return {
        "video_id": script["video_id"],
        "account": account.handle,
        "video_style": "arcads_avatar",
        "platform": "arcads",
        "payload": payload,
        "selection": {
            "avatar": {
                "id": avatar["id"],
                "vibe": avatar.get("vibe"),
                "environment": avatar.get("environment"),
            },
            "voice": {"id": voice["id"], "tone": voice.get("tone")},
            "rotation_key": variant_index % max(
                len(cfg["avatar_pool"]), len(cfg["voice_pool"])
            ),
        },
        "metadata": {
            "source_script_video_id": script["video_id"],
            "source_product_id": script.get("source_product_id"),
            "source_pattern_id": script.get("source_pattern_id"),
            "target_duration_seconds": script.get("target_duration_seconds"),
            "caption": script.get("caption"),
            "hashtags": script.get("hashtags", []),
        },
    }
