"""Two-stage hook analysis:

  Stage 1 — identify_hooks: per video, extract the literal 0-3s opening text
            and tag its hook type. Stateless, parallelizable across batches.
  Stage 2 — cluster_patterns: collapse the identified hooks into reusable
            templates, persona-aware, banned-phrase-filtered.

Both stages call Claude through the shared client. Engagement metrics and
banned-phrase filtering are computed locally for auditability.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from agents.hooks.prompts import (
    HOOK_IDENTIFICATION_SYSTEM,
    HOOK_PATTERN_CLUSTER_SYSTEM,
    hook_identification_user_prompt,
    hook_pattern_cluster_user_prompt,
)
from core.config_loader import AccountConfig
from core.logger import get_logger
from integrations.claude_api import ClaudeClient

# Identification is straightforward classification — chunk to keep token use
# bounded and the response shape predictable.
_IDENT_BATCH_SIZE = 25


def _engagement_rate(v: dict[str, Any]) -> float:
    views = max(1, int(v.get("view_count", 0)))
    interactions = (
        int(v.get("like_count", 0))
        + int(v.get("comment_count", 0))
        + int(v.get("share_count", 0))
    )
    return interactions / views


def _chunk(seq: list[Any], n: int) -> list[list[Any]]:
    return [seq[i : i + n] for i in range(0, len(seq), n)]


def identify_hooks(
    account: AccountConfig,
    videos: list[dict[str, Any]],
    claude: ClaudeClient,
    model: str,
) -> list[dict[str, Any]]:
    """Returns videos with `hook`, `hook_type`, `confidence`, `engagement_rate`
    fields filled in. Drops videos Claude couldn't classify confidently.
    """
    log = get_logger("hooks.extractor", account.handle)
    if not videos:
        return []

    indexed = {v["video_id"]: v for v in videos}
    out: list[dict[str, Any]] = []

    for batch in _chunk(videos, _IDENT_BATCH_SIZE):
        payload = [
            {
                "video_id": v["video_id"],
                "caption": v.get("caption", ""),
                "first_line_transcript": v.get("first_line_transcript"),
                "on_screen_text": v.get("on_screen_text"),
            }
            for v in batch
        ]
        raw = claude.complete_json(
            model=model,
            system=HOOK_IDENTIFICATION_SYSTEM,
            user=hook_identification_user_prompt(payload),
        )
        for row in raw.get("videos", []):
            src = indexed.get(row.get("video_id"))
            if not src:
                continue
            if not row.get("hook") or float(row.get("confidence", 0)) < 0.5:
                continue
            out.append({
                **src,
                "hook": row["hook"].strip(),
                "hook_type": row.get("hook_type", "other"),
                "confidence": float(row.get("confidence", 0.0)),
                "engagement_rate": round(_engagement_rate(src), 5),
            })

    log.info("identified hooks", extra={"input": len(videos), "kept": len(out)})
    return out


def _contains_banned(text: str, banned: list[str]) -> bool:
    lower = text.lower()
    return any(b.lower() in lower for b in banned)


def cluster_patterns(
    account: AccountConfig,
    identified_hooks: list[dict[str, Any]],
    yesterday_winners: list[dict[str, Any]] | None,
    claude: ClaudeClient,
    model: str,
) -> list[dict[str, Any]]:
    """Cluster `identified_hooks` into reusable patterns. Augment Claude's
    output with measured engagement (per-pattern avg) so the scriptwriter has
    both the qualitative score and the empirical signal.
    """
    log = get_logger("hooks.extractor", account.handle)
    if not identified_hooks:
        log.info("no hooks to cluster")
        return []

    banned = list(account.persona.get("banned_phrases", []))
    hook_payload = [
        {
            "video_id": h["video_id"],
            "hook": h["hook"],
            "hook_type": h.get("hook_type", "other"),
            "view_count": h.get("view_count", 0),
            "like_count": h.get("like_count", 0),
            "comment_count": h.get("comment_count", 0),
            "share_count": h.get("share_count", 0),
            "engagement_rate": h["engagement_rate"],
        }
        for h in identified_hooks
    ]

    raw = claude.complete_json(
        model=model,
        system=HOOK_PATTERN_CLUSTER_SYSTEM,
        user=hook_pattern_cluster_user_prompt(
            persona=account.persona,
            banned_phrases=banned,
            hooks=hook_payload,
            yesterday_winners=yesterday_winners,
        ),
    )

    er_by_id = {h["video_id"]: h["engagement_rate"] for h in identified_hooks}
    patterns_out: list[dict[str, Any]] = []
    for p in raw.get("patterns", []):
        # Local banned-phrase enforcement — never trust the model alone.
        if _contains_banned(p.get("template", ""), banned) or any(
            _contains_banned(ex, banned) for ex in p.get("examples", [])
        ):
            log.info("dropped pattern using banned phrase", extra={"pattern": p.get("id")})
            continue

        source_ids = p.get("source_video_ids", []) or []
        engagement_rates = [er_by_id[sid] for sid in source_ids if sid in er_by_id]
        avg_er = sum(engagement_rates) / len(engagement_rates) if engagement_rates else 0.0

        patterns_out.append({
            "id": p.get("id"),
            "category": p.get("category", "other"),
            "template": p.get("template", ""),
            "examples": p.get("examples", []),
            "source_video_ids": source_ids,
            "source_video_count": len(source_ids),
            "avg_engagement_rate": round(avg_er, 5),
            "claude_score": float(p.get("claude_score", 0.0)),
            "persona_fit_notes": p.get("persona_fit_notes", ""),
            "echoes_yesterday_winner": bool(p.get("echoes_yesterday_winner", False)),
        })

    # Final ranking: blend qualitative score with measured engagement, with a
    # bonus for echoing yesterday's winners.
    def _final(p: dict[str, Any]) -> float:
        return (
            0.55 * p["claude_score"]
            + 0.35 * min(1.0, p["avg_engagement_rate"] * 10)  # rescale ER to ~0-1
            + (0.10 if p["echoes_yesterday_winner"] else 0.0)
        )

    for p in patterns_out:
        p["final_score"] = round(_final(p), 4)
    patterns_out.sort(key=lambda p: p["final_score"], reverse=True)

    by_category: dict[str, int] = defaultdict(int)
    for p in patterns_out:
        by_category[p["category"]] += 1
    log.info(
        "clustered patterns",
        extra={"count": len(patterns_out), "by_category": dict(by_category)},
    )
    return patterns_out
