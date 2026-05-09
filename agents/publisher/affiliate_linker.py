"""Builds the per-video link plan for the publisher.

Two link plans:

  tiktok_shop_affiliate (sharpguylab, rideupgrades)
    - Builds a TikTok Shop affiliate URL from product_id + per-account aff id.
    - Bio link target  : the affiliate URL (manual one-time set; we record it
                         so a bio-bot or human can update it).
    - Pinned comment   : "🛒 link → <affiliate_url>"  (comment endpoint may
                         be unavailable; publisher records the intent either way).

  subscription (passivepoly)
    - Bio link target  : passivepoly.com (already on-screen too).
    - Pinned comment   : "join → <whop_url>"
    - In-video CTA     : passivepoly.com (rendered by Agent 6 — we don't add
                         it again here).

The plan is a pure data structure. The publisher decides what to actually do
with it given the account's link_strategy and the API's capabilities.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.config_loader import AccountConfig


@dataclass
class LinkPlan:
    type: str                                # "tiktok_shop_affiliate" | "subscription"
    bio_url: str | None = None
    pinned_comment_text: str | None = None
    in_caption_appendix: str | None = None   # text appended to the post caption (no URL)
    metadata: dict[str, Any] = field(default_factory=dict)


class AffiliateLinkerError(RuntimeError):
    pass


def build_link_plan(
    *,
    account: AccountConfig,
    script_metadata: dict[str, Any],
) -> LinkPlan:
    monetization = account.monetization or {}
    mtype = monetization.get("type")

    if mtype == "tiktok_shop_affiliate":
        return _build_affiliate_plan(account, monetization, script_metadata)
    if mtype == "subscription":
        return _build_subscription_plan(account, monetization, script_metadata)
    raise AffiliateLinkerError(f"unknown monetization type: {mtype!r}")


def _build_affiliate_plan(
    account: AccountConfig,
    monetization: dict[str, Any],
    script_metadata: dict[str, Any],
) -> LinkPlan:
    creds = account.raw.get("api_credentials") or {}
    affiliate_id = creds.get("tiktok_shop_affiliate_id", "")
    if not affiliate_id:
        raise AffiliateLinkerError(
            f"@{account.handle}: tiktok_shop_affiliate_id missing — set "
            "TIKTOK_SHOP_AFFILIATE_ID_<HANDLE> in env"
        )

    product_id = script_metadata.get("source_product_id")
    if not product_id:
        raise AffiliateLinkerError(
            "no source_product_id on script metadata — cannot build affiliate URL"
        )

    template = monetization.get(
        "affiliate_url_template",
        "https://www.tiktok.com/view/product/{product_id}?aff_id={affiliate_id}",
    )
    affiliate_url = template.format(product_id=product_id, affiliate_id=affiliate_id)

    return LinkPlan(
        type="tiktok_shop_affiliate",
        bio_url=affiliate_url,
        pinned_comment_text=f"🛒 link → {affiliate_url}",
        in_caption_appendix=None,           # don't shove the URL into the caption
        metadata={
            "affiliate_url": affiliate_url,
            "product_id": product_id,
            "commission_min": monetization.get("commission_min"),
            "commission_max": monetization.get("commission_max"),
        },
    )


def _build_subscription_plan(
    account: AccountConfig,
    monetization: dict[str, Any],
    script_metadata: dict[str, Any],
) -> LinkPlan:
    cta_url = monetization.get("cta_url") or script_metadata.get("cta_url")
    whop_url = monetization.get("whop_url")
    if not cta_url:
        raise AffiliateLinkerError(
            f"@{account.handle}: subscription monetization missing cta_url"
        )

    pinned = f"join → {whop_url}" if whop_url else None

    return LinkPlan(
        type="subscription",
        bio_url=cta_url,
        pinned_comment_text=pinned,
        in_caption_appendix=None,           # CTA is already on-screen (Agent 6)
        metadata={
            "cta_url": cta_url,
            "whop_url": whop_url,
            "platform": monetization.get("platform"),
        },
    )
