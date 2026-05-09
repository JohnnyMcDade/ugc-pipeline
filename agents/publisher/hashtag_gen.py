"""Publish-time hashtag finalization.

The script writer (Agent 3) already produced persona-curated hashtags. This
module layers the latest signals on top:

  1. Start with `script.hashtags` (persona-aware, most specific).
  2. Append `account.hashtags.evergreen` (always-on, niche-defining).
  3. Append top hashtags from yesterday's winners (Agent 8 output) if present.
  4. Remove anything in `account.hashtags.exclude`.
  5. Normalize, dedupe, cap at `account.hashtags.max` (default 6).

Order matters — earlier entries win on dedupe so the script's specific tags
appear before generic evergreen tags.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from core.config_loader import AccountConfig
from core.dateutils import yesterday_str

ANALYTICS_ROOT = Path("data/analytics")

_TAG_RE = re.compile(r"#?[A-Za-z0-9_]+")


def _normalize(tag: str) -> str | None:
    """Normalize to lowercase #hashtag form. Returns None if not a valid tag."""
    if not tag:
        return None
    tag = tag.strip()
    if not tag.startswith("#"):
        tag = "#" + tag
    m = _TAG_RE.fullmatch(tag.lstrip("#"))
    if not m:
        return None
    return "#" + tag.lstrip("#").lower()


def _yesterday_winner_tags(handle: str, top_n: int = 3) -> list[str]:
    path = ANALYTICS_ROOT / handle / yesterday_str() / "winners.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []

    counts: dict[str, int] = {}
    for w in data.get("winners", []):
        for t in w.get("hashtags") or []:
            n = _normalize(t)
            if n:
                counts[n] = counts.get(n, 0) + 1
    # Sort by frequency desc, take top N.
    return [t for t, _ in sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:top_n]]


def finalize(
    *,
    account: AccountConfig,
    script_hashtags: list[str],
) -> tuple[list[str], dict[str, Any]]:
    """Returns (final_tag_list, debug_info)."""
    cfg = account.raw.get("hashtags") or {}
    evergreen = cfg.get("evergreen") or []
    exclude = {n for n in (_normalize(t) for t in (cfg.get("exclude") or [])) if n}
    max_tags = int(cfg.get("max", 6))

    sources: list[tuple[str, list[str]]] = [
        ("script", script_hashtags or []),
        ("evergreen", evergreen),
        ("yesterday_winners", _yesterday_winner_tags(account.handle)),
    ]

    seen: set[str] = set()
    out: list[str] = []
    debug = {"sources": {name: [] for name, _ in sources}, "excluded": []}

    for name, tags in sources:
        for t in tags:
            n = _normalize(t)
            if not n:
                continue
            if n in exclude:
                debug["excluded"].append(n)
                continue
            if n in seen:
                continue
            seen.add(n)
            out.append(n)
            debug["sources"][name].append(n)
            if len(out) >= max_tags:
                debug["capped"] = True
                return out, debug

    debug["capped"] = False
    return out, debug
