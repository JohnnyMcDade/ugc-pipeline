"""Discord webhook alerter for Agent 10.

Two surfaces:
  - send_alert(title, level, fields)  — used by monitor.py when auto-repair
                                        gives up. Posts to DISCORD_WEBHOOK_REPAIR.
  - send_report(title, sections)      — used by report.py for the daily 7am
                                        summary. Same webhook.

Webhook URL is read once at module import. If DISCORD_WEBHOOK_REPAIR is
unset, alerts are logged to stdout instead and never POST'd — pipeline
keeps running.
"""

from __future__ import annotations

import os
from typing import Any

import requests

from core.logger import get_logger

_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_REPAIR", "").strip()
_TIMEOUT = 10

# Discord embed color codes (decimal).
_COLORS = {
    "red":    0xE74C3C,
    "yellow": 0xF1C40F,
    "green":  0x2ECC71,
    "blue":   0x3498DB,
    "grey":   0x95A5A6,
}


def _post(payload: dict[str, Any]) -> bool:
    """POST `payload` to the configured webhook. Returns True on 2xx."""
    log = get_logger("health.alerter")
    if not _WEBHOOK_URL:
        log.warning(
            "DISCORD_WEBHOOK_REPAIR not set — alert/report not sent",
            extra={"payload_preview": str(payload)[:300]},
        )
        return False
    try:
        r = requests.post(_WEBHOOK_URL, json=payload, timeout=_TIMEOUT)
        if r.status_code >= 400:
            log.error("discord webhook returned %d: %s", r.status_code, r.text[:300])
            return False
        return True
    except requests.RequestException as e:
        log.error("discord webhook network error", extra={"err": str(e)})
        return False


def send_alert(
    *,
    title: str,
    level: str = "red",
    description: str | None = None,
    fields: list[dict[str, str]] | None = None,
    footer: str | None = None,
) -> bool:
    """Send a single auto-repair-failed alert. `level` ∈ {red, yellow, green, blue}."""
    embed: dict[str, Any] = {
        "title": title[:256],
        "color": _COLORS.get(level, _COLORS["red"]),
    }
    if description:
        embed["description"] = description[:4000]
    if fields:
        # Discord allows max 25 fields per embed.
        embed["fields"] = [
            {"name": f["name"][:256], "value": f["value"][:1024],
             "inline": bool(f.get("inline", False))}
            for f in fields[:25]
        ]
    if footer:
        embed["footer"] = {"text": footer[:2048]}
    return _post({"embeds": [embed]})


def send_report(*, title: str, sections: list[tuple[str, str]]) -> bool:
    """Send a daily-style multi-section report. `sections` is a list of
    (heading, body) tuples — rendered as Discord fields.
    """
    return send_alert(
        title=title,
        level="blue",
        fields=[{"name": h, "value": b, "inline": False} for h, b in sections],
        footer="UGC pipeline · daily health report",
    )
