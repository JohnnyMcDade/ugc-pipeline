"""Persona-aware local validation for generated scripts.

Claude is told the rules, but we never trust the model alone — banned phrases,
duration estimates, and CTA-URL identity are checked here in pure Python so
violations surface deterministically and are auditable.

Validation is per-script and returns a list of issues. Empty list = clean.
The writer drops or keeps invalid scripts based on `scriptwriter.drop_invalid`
in master.yaml.
"""

from __future__ import annotations

import re
from typing import Any

from core.config_loader import AccountConfig

_WORD_RE = re.compile(r"\b[\w'-]+\b")


def estimate_duration_seconds(text: str, words_per_minute: int) -> float:
    if not text:
        return 0.0
    word_count = len(_WORD_RE.findall(text))
    return word_count / (words_per_minute / 60.0)


def contains_banned(text: str, banned: list[str]) -> list[str]:
    """Returns the banned phrases found in `text` (lowercase substring match)."""
    if not text:
        return []
    lower = text.lower()
    return [b for b in banned if b.lower() in lower]


def _all_text_fields(script: dict[str, Any]) -> list[str]:
    """Every text surface a viewer or model could see. The script schema is
    unified now (all accounts produce avatar UGC via HeyGen), so the same
    field set applies regardless of style.
    """
    out: list[str] = []
    if script.get("voiceover_text"):
        out.append(script["voiceover_text"])
    if script.get("caption"):
        out.append(script["caption"])
    if script.get("hook"):
        out.append(script["hook"])
    for beat in script.get("body_beats", []) or []:
        if beat.get("text"):
            out.append(beat["text"])
    # passivepoly: evidence_payload.headline shows up burned into the screenshot.
    headline = (script.get("evidence_payload") or {}).get("headline")
    if headline:
        out.append(headline)
    return out


def _required_fields(monetization_type: str) -> list[str]:
    common = [
        "variant_index", "source_pattern_id", "voiceover_text", "caption", "hashtags",
        "hook", "body_beats", "target_duration_seconds",
    ]
    if monetization_type == "tiktok_shop_affiliate":
        return common + ["source_product_id"]
    if monetization_type == "subscription":
        return common + ["category", "cta_url"]   # source_signal_id may be null for educational
    return common


def validate_script(
    script: dict[str, Any],
    account: AccountConfig,
    target_duration_seconds: int,
    duration_tolerance_seconds: int,
    words_per_minute: int,
    max_hashtags: int,
    valid_pattern_ids: set[str],
    valid_source_ids: set[str],
    cta_url_required: str | None = None,
) -> list[str]:
    issues: list[str] = []
    monetization_type = (account.monetization or {}).get("type", "")

    # 1. Required fields.
    for f in _required_fields(monetization_type):
        if f not in script or script[f] in (None, "", []):
            issues.append(f"missing field: {f}")

    # 2. Banned phrases anywhere a viewer or system reads the text.
    banned = list(account.persona.get("banned_phrases", []))
    for surface in _all_text_fields(script):
        hits = contains_banned(surface, banned)
        if hits:
            issues.append(f"banned phrase(s) present: {sorted(set(hits))}")
            break  # one report is enough; don't spam

    # 3. Provenance: pattern + source ids must be ones we provided.
    if "source_pattern_id" in script and script["source_pattern_id"] not in valid_pattern_ids:
        issues.append(f"unknown source_pattern_id: {script['source_pattern_id']!r}")
    if monetization_type == "tiktok_shop_affiliate":
        if script.get("source_product_id") not in valid_source_ids:
            issues.append(f"unknown source_product_id: {script.get('source_product_id')!r}")
    elif monetization_type == "subscription":
        sig = script.get("source_signal_id")
        # `null` is allowed for the educational category — concept-only videos
        # don't anchor to a specific live event.
        if sig and sig not in valid_source_ids:
            issues.append(f"unknown source_signal_id: {sig!r}")

    # 4. Duration in tolerance.
    est = estimate_duration_seconds(script.get("voiceover_text", ""), words_per_minute)
    target = int(script.get("target_duration_seconds") or target_duration_seconds)
    if abs(est - target) > duration_tolerance_seconds:
        issues.append(
            f"duration estimate {est:.1f}s outside ±{duration_tolerance_seconds}s of target {target}s"
        )

    # 5. Hashtag cap.
    tags = script.get("hashtags") or []
    if len(tags) > max_hashtags:
        issues.append(f"too many hashtags: {len(tags)} > {max_hashtags}")

    # 6. CTA URL identity (passivepoly only).
    if cta_url_required is not None:
        if script.get("cta_url") != cta_url_required:
            issues.append(
                f"cta_url must equal {cta_url_required!r}, got {script.get('cta_url')!r}"
            )

    return issues
