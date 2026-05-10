"""Deterministic HeyGen v2 payload formatter (no LLM call).

Maps a validated script dict + the account's `videogen.heygen` config into a
ready-to-POST HeyGen body. Selection rules:

  Avatar:  rotated by `variant_index % len(pool)` so the four daily variants
           use different avatars and the rotation is stable across re-runs.
  Voice:   rotated the same way.
  Pacing:  HeyGen exposes `voice.speed` (0.5–1.5). Derived from hook_type
           (the same lookup the prior Arcads formatter used), falling back
           to account default. We map "fast/natural/slow" → speed values.
  Background: comes from per-account YAML (color or image URL). Same
           background per account — variation is in avatar choice, not setting.

Output is the structured doc Agent 5 (videogen.generator) hands to the
HeyGen client. The `payload` field is the literal v2/video/generate body.
"""

from __future__ import annotations

from typing import Any

from core.config_loader import AccountConfig

# Hook-type → (avatar_style hint, voice_speed). avatar_style is HeyGen's
# framing parameter ("normal" | "circle" | "closeUp"). closeUp suits punchy
# delivery; normal is conversational; circle works for over-the-shoulder
# explainer-style.
_HOOK_TYPE_DELIVERY = {
    "POV":          ("closeUp", 1.0),
    "contrarian":   ("closeUp", 1.1),
    "curiosity":    ("normal",  0.95),
    "social_proof": ("normal",  1.0),
    "identity":     ("closeUp", 1.1),
    "numerical":    ("normal",  1.0),
    "question":     ("normal",  1.0),
    "other":        (None,      None),
}

_PACING_TO_SPEED = {"slow": 0.9, "natural": 1.0, "fast": 1.1}


class HeyGenConfigError(RuntimeError):
    pass


def _heygen_cfg(account: AccountConfig) -> dict[str, Any]:
    cfg = (account.raw.get("videogen") or {}).get("heygen") or {}
    if not cfg.get("avatar_pool"):
        raise HeyGenConfigError(
            f"@{account.handle}: videogen.heygen.avatar_pool is empty. "
            "Add at least one entry with avatar_id (from /v2/avatars) and a vibe label."
        )
    if not cfg.get("voice_pool"):
        raise HeyGenConfigError(
            f"@{account.handle}: videogen.heygen.voice_pool is empty. "
            "Add at least one entry with voice_id (from /v2/voices)."
        )
    return cfg


def _select_by_index(pool: list[dict[str, Any]], variant_index: int) -> dict[str, Any]:
    return pool[variant_index % len(pool)]


def _derive_delivery(
    script: dict[str, Any],
    cfg: dict[str, Any],
) -> tuple[str, float]:
    """Returns (avatar_style, voice_speed)."""
    default_style = cfg.get("default_avatar_style", "normal")
    default_pacing = cfg.get("default_pacing", "natural")
    default_speed = _PACING_TO_SPEED.get(default_pacing, 1.0)

    hook_type = (script.get("pattern_category") or "other").lower()
    style_hint, speed_hint = _HOOK_TYPE_DELIVERY.get(hook_type, (None, None))
    return (style_hint or default_style, speed_hint or default_speed)


def _build_background(cfg: dict[str, Any]) -> dict[str, Any]:
    """HeyGen accepts {type: 'color', value: '#hex'} or {type: 'image',
    url: '...'}. Per-account YAML supplies one or the other.
    """
    bg = cfg.get("background") or {"type": "color", "value": "#1a1a1a"}
    out = {"type": bg.get("type", "color")}
    if bg["type"] == "color":
        out["value"] = bg.get("value", "#1a1a1a")
    elif bg["type"] == "image":
        out["url"] = bg.get("url") or bg.get("value")
    elif bg["type"] == "video":
        out["url"] = bg.get("url") or bg.get("value")
        out["play_style"] = bg.get("play_style", "loop")
    return out


def format_heygen_payload(
    script: dict[str, Any],
    account: AccountConfig,
) -> dict[str, Any]:
    """Returns the doc Agent 5 hands to HeyGenClient. The `payload` field is
    the literal POST /v2/video/generate body.
    """
    cfg = _heygen_cfg(account)
    variant_index = int(script.get("variant_index", 0))

    avatar = _select_by_index(cfg["avatar_pool"], variant_index)
    voice = _select_by_index(cfg["voice_pool"], variant_index)
    avatar_style, voice_speed = _derive_delivery(script, cfg)

    dimension = cfg.get("dimension") or {"width": 1080, "height": 1920}
    background = _build_background(cfg)

    video_input: dict[str, Any] = {
        "character": {
            "type": "avatar",
            "avatar_id": avatar["avatar_id"],
            "avatar_style": avatar.get("avatar_style") or avatar_style,
        },
        "voice": {
            "type": "text",
            "input_text": script["voiceover_text"],
            "voice_id": voice["voice_id"],
            "speed": float(voice.get("speed") or voice_speed),
        },
        "background": background,
    }

    payload = {
        "video_inputs": [video_input],
        "dimension": dimension,
        # `test=True` while developing avoids burning real generation credits.
        "test": bool(cfg.get("test", False)),
        # Use the pipeline's video_id as a callback_id so we can correlate
        # webhook events back to our records if/when webhooks are wired.
        "callback_id": script.get("video_id"),
    }

    return {
        "video_id": script["video_id"],
        "account": account.handle,
        "video_style": "heygen_avatar",
        "platform": "heygen",
        "payload": payload,
        "selection": {
            "avatar": {
                "avatar_id": avatar["avatar_id"],
                "vibe": avatar.get("vibe"),
                "avatar_style": video_input["character"]["avatar_style"],
            },
            "voice": {
                "voice_id": voice["voice_id"],
                "tone": voice.get("tone"),
                "speed": video_input["voice"]["speed"],
            },
            "rotation_key": variant_index % max(
                len(cfg["avatar_pool"]), len(cfg["voice_pool"])
            ),
        },
        "evidence_screenshot_required": bool(script.get("evidence_screenshot_required")),
        "evidence_payload": script.get("evidence_payload"),
        "evidence_show_at_seconds": script.get("evidence_show_at_seconds"),
        "evidence_show_duration_seconds": script.get("evidence_show_duration_seconds"),
        "metadata": {
            "source_script_video_id": script["video_id"],
            "source_product_id": script.get("source_product_id"),
            "source_signal_id": script.get("source_signal_id"),
            "source_pattern_id": script.get("source_pattern_id"),
            "category": script.get("category"),
            "target_duration_seconds": script.get("target_duration_seconds"),
            "caption": script.get("caption"),
            "hashtags": script.get("hashtags", []),
            "cta_url": script.get("cta_url"),
        },
    }
