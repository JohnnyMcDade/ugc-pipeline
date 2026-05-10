#!/usr/bin/env python3
"""Mock-mode dry run for @sharpguylab — Agents 1-6.

Real Anthropic API calls for Agents 1, 2, and 3 (the intelligence layer).
Mocked fixtures stand in for the two unwired TikTok scrapers. Agent 4 runs
real (deterministic, no API). Agents 5-6 are not invoked.

Run: python dry_run.py

Will consume some Anthropic credits (~$0.10-0.30 total). No HeyGen credits,
no TikTok posting, no ffmpeg.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

from dotenv import load_dotenv
# override=True because some shells export empty placeholders (e.g.
# ANTHROPIC_API_KEY="") that would otherwise shadow our .env values.
load_dotenv(override=True)

# ── ANSI ────────────────────────────────────────────────────────────────────
RESET   = "\033[0m"
BOLD    = "\033[1m"
DIM     = "\033[2m"
CYAN    = "\033[36m"
GREEN   = "\033[32m"
YELLOW  = "\033[33m"
MAGENTA = "\033[35m"
GREY    = "\033[90m"


def banner(text: str) -> None:
    line = "═" * 70
    print()
    print(f"{CYAN}{line}{RESET}")
    print(f"{BOLD}{CYAN}  {text}{RESET}")
    print(f"{CYAN}{line}{RESET}")


def step(n: int, title: str) -> None:
    print()
    print(f"{BOLD}{CYAN}━━━ Agent {n} ━━━ {RESET}{BOLD}{title}{RESET}")
    print(f"{CYAN}{'─' * 70}{RESET}")


# ── Fixtures: stand in for the two unwired scrapers ─────────────────────────

FIXTURES_PRODUCTS: list[dict[str, Any]] = [
    {
        "product_id": "cc_velvetier_001",
        "title": "Velvetier Beard Oil — Sandalwood & Vanilla",
        "category": "mens_grooming",
        "price_usd": 24.99,
        "commission_pct": 0.18,
        "post_count_7d": 412,
        "post_count_prev_7d": 98,
        "rating": 4.6,
        "url": "https://shop.tiktok.com/view/product/p_velvetier_001",
    },
    {
        "product_id": "cc_notum_002",
        "title": "Notum DateNight Cologne — vanilla / sandalwood / amber",
        "category": "mens_grooming",
        "price_usd": 32.50,
        "commission_pct": 0.22,
        "post_count_7d": 1840,
        "post_count_prev_7d": 510,
        "rating": 4.7,
        "url": "https://shop.tiktok.com/view/product/p_notum_002",
    },
    {
        "product_id": "cc_sharpedge_003",
        "title": "Sharp Edge Beard Trimmer Kit — 12 guards, 60min charge",
        "category": "mens_grooming",
        "price_usd": 39.99,
        "commission_pct": 0.20,
        "post_count_7d": 280,
        "post_count_prev_7d": 240,
        "rating": 4.4,
        "url": "https://shop.tiktok.com/view/product/p_sharpedge_003",
    },
    {
        "product_id": "cc_dewslate_004",
        "title": "Dewslate Hair Clay — matte, all-day hold",
        "category": "mens_grooming",
        "price_usd": 18.00,
        "commission_pct": 0.15,
        "post_count_7d": 95,
        "post_count_prev_7d": 80,
        "rating": 4.5,
        "url": "https://shop.tiktok.com/view/product/p_dewslate_004",
    },
    {
        "product_id": "cc_freshman_005",
        "title": "Freshman Body Wash — cedar & black pepper, 24h scent",
        "category": "mens_grooming",
        "price_usd": 16.50,
        "commission_pct": 0.16,
        "post_count_7d": 720,
        "post_count_prev_7d": 220,
        "rating": 4.5,
        "url": "https://shop.tiktok.com/view/product/p_freshman_005",
    },
    {
        "product_id": "cc_glowlab_006",
        "title": "GlowLab Men's Vitamin C Serum — daily skincare",
        "category": "mens_grooming",
        "price_usd": 28.00,
        "commission_pct": 0.19,
        "post_count_7d": 156,
        "post_count_prev_7d": 130,
        "rating": 4.3,
        "url": "https://shop.tiktok.com/view/product/p_glowlab_006",
    },
]


def _vid(vid_id: str, author: str, caption: str, views: int, likes: int,
         comments: int, shares: int, duration: int, tags: list[str]) -> dict[str, Any]:
    return {
        "video_id": vid_id, "url": f"https://tiktok.com/@{author}/video/{vid_id}",
        "author": author, "caption": caption,
        "first_line_transcript": caption.split("...")[0][:60],
        "on_screen_text": caption.split(" 🌹")[0].split(" ✨")[0],
        "view_count": views, "like_count": likes,
        "comment_count": comments, "share_count": shares,
        "duration_seconds": duration, "posted_at": "2026-04-28T14:00:00Z",
        "music": "trending_audio_001", "hashtags": tags,
    }


FIXTURES_VIDEOS: list[dict[str, Any]] = [
    _vid("v_001", "grooming.lounge",
         "POV: she smelled my neck and asked what I was wearing 🌹",
         1_240_000, 98_400, 4_280, 8_910, 22, ["#cologne", "#fyp", "#mensgrooming"]),
    _vid("v_002", "manmade",
         "POV: you finally found a beard oil that doesn't smell like dad",
         680_000, 52_000, 2_100, 4_200, 28, ["#beard", "#fyp"]),
    _vid("v_003", "grooming.lounge",
         "stop using your roommate's body wash. seriously.",
         920_000, 71_000, 3_400, 6_500, 25, ["#fyp", "#mensgrooming"]),
    _vid("v_004", "manmade",
         "stop putting cologne on your wrists, here's where to spray it",
         480_000, 35_000, 1_800, 3_200, 30, ["#cologne", "#fragrance"]),
    _vid("v_005", "grooming.lounge",
         "if you're 22 and still using Axe, this is for you",
         760_000, 61_000, 4_900, 5_200, 22, ["#fyp", "#dating"]),
    _vid("v_006", "manmade",
         "if you have a beard and you're not using oil, watch this",
         530_000, 41_000, 1_900, 3_800, 26, ["#beardcare"]),
    _vid("v_007", "grooming.lounge",
         "the one cologne hack nobody tells you about",
         1_100_000, 85_000, 3_900, 7_800, 30, ["#cologne", "#fyp"]),
    _vid("v_008", "manmade",
         "the trimmer guard size nobody uses (but should)",
         320_000, 24_000, 1_200, 1_900, 27, ["#beard", "#mensgrooming"]),
    _vid("v_009", "grooming.lounge",
         "3 colognes under $40 that smell like you spent $200",
         950_000, 76_000, 5_100, 9_200, 35, ["#cologne", "#fyp", "#fragrance"]),
    _vid("v_010", "manmade",
         "why does no one talk about scent layering?",
         280_000, 20_000, 1_400, 1_500, 32, ["#fragrance"]),
    _vid("v_011", "grooming.lounge",
         "I tried this beard oil for 30 days, here's what happened",
         620_000, 47_000, 2_200, 3_400, 38, ["#beard", "#fyp"]),
    _vid("v_012", "manmade",
         "I tried 7 colognes my barber recommended, ranked them",
         410_000, 32_000, 1_900, 2_700, 42, ["#cologne", "#fragrance"]),
    _vid("v_013", "grooming.lounge",
         "POV: she keeps hugging you because of the smell",
         880_000, 68_000, 3_100, 5_800, 20, ["#cologne", "#dating", "#fyp"]),
    _vid("v_014", "thatdudecancook",
         "my grooming routine for date night",
         65_000, 3_200, 140, 95, 45, ["#mensgrooming"]),
    _vid("v_015", "manmade",
         "review of the Sharp Edge trimmer kit",
         88_000, 4_100, 180, 120, 60, ["#review", "#beard"]),
]


# ── Mocks ───────────────────────────────────────────────────────────────────

def fake_cc_fetch(self, keyword: str) -> list[dict[str, Any]]:
    """Stand-in for tiktok_creative_center._fetch_raw."""
    kw = keyword.lower()
    matches = [p for p in FIXTURES_PRODUCTS
               if any(w in p["title"].lower() for w in kw.split())]
    return matches or FIXTURES_PRODUCTS[:3]


def fake_scraper_username(self, username: str, limit: int) -> list[dict[str, Any]]:
    handle = username.lstrip("@").lower()
    matches = [v for v in FIXTURES_VIDEOS if v["author"].lower() == handle]
    return matches[:limit] if matches else FIXTURES_VIDEOS[:limit]


def fake_scraper_keyword(self, keyword: str, limit: int) -> list[dict[str, Any]]:
    matches = [v for v in FIXTURES_VIDEOS if keyword.lower() in v["caption"].lower()]
    return matches[:limit] if matches else FIXTURES_VIDEOS[:limit]


# ── Main ───────────────────────────────────────────────────────────────────

def main() -> None:
    # Patch before importing the agent runners.
    from integrations.tiktok_creative_center import CreativeCenterClient
    from integrations.tiktok_scraper import TikTokScraperClient

    patches = [
        patch.object(CreativeCenterClient, "_fetch_raw", fake_cc_fetch),
        patch.object(TikTokScraperClient, "top_videos_by_username", fake_scraper_username),
        patch.object(TikTokScraperClient, "top_videos_by_keyword", fake_scraper_keyword),
    ]
    for p in patches:
        p.start()

    try:
        from core.config_loader import load_pipeline
        from core.dateutils import today_str
        from agents.scout.scout import run as scout_run
        from agents.hooks.analyzer import run as hooks_run
        from agents.scriptwriter.writer import run as scriptwriter_run
        from agents.videoprompt.engineer import run as videoprompt_run
        from agents.publisher.hashtag_gen import finalize as finalize_hashtags

        pipeline = load_pipeline(Path("config"))
        account = pipeline.account("sharpguylab")

        banner("DRY RUN — @sharpguylab — Agents 1-6 (mock mode)")
        print(f"  {DIM}real Anthropic calls: Agents 1, 2, 3{RESET}")
        print(f"  {DIM}mocked: TikTok scrapers (fixtures), HeyGen, ffmpeg{RESET}")

        # ── AGENT 1 ────────────────────────────────────────────────────────
        step(1, "Trend & product scout  (REAL Claude — Sonnet 4.6)")
        r1 = scout_run(account, {"slot": "scout"})
        products = r1.get("products", [])
        print(f"  {GREEN}✓{RESET} scored {len(products)} products above threshold "
              f"({r1.get('min_score', '?')})")
        print()
        for p in products:
            print(f"  {GREEN}•{RESET} {BOLD}{p['title']}{RESET}")
            print(f"      {DIM}score={RESET}{MAGENTA}{p['score']:.3f}{RESET}  "
                  f"{DIM}price={RESET}${p['price_usd']}  "
                  f"{DIM}commission={RESET}{int(p['commission_pct']*100)}%")
            ax = p.get("axis_scores", {})
            print(f"      {DIM}axes:{RESET}  "
                  f"velocity={ax.get('velocity', '?')}  "
                  f"relevance={ax.get('relevance', '?')}  "
                  f"commission={ax.get('commission', '?')}  "
                  f"saturation_penalty={ax.get('saturation_penalty', '?')}")
            print(f"      {DIM}rationale:{RESET}   {p.get('rationale', '')}")
            print(f"      {DIM}hook angle:{RESET}  {YELLOW}{p.get('hook_angle', '')}{RESET}")
            print()

        # ── AGENT 2 ────────────────────────────────────────────────────────
        step(2, "Hook researcher  (REAL Claude — 2 batched calls)")
        r2 = hooks_run(account, {"slot": "hooks"})
        patterns = r2.get("patterns", [])
        print(f"  {GREEN}✓{RESET} extracted {len(patterns)} clusters from "
              f"{r2.get('hooks_identified', '?')} identified hooks "
              f"(input {r2.get('source_videos_analyzed', '?')} videos)")
        print()
        for pat in patterns:
            print(f"  {GREEN}•{RESET} {BOLD}{pat['id']}{RESET}  "
                  f"{DIM}[{pat['category']}]{RESET}  "
                  f"{DIM}final_score={RESET}{MAGENTA}{pat['final_score']:.3f}{RESET}  "
                  f"{DIM}(claude={RESET}{pat['claude_score']}{DIM}, "
                  f"avg_er={RESET}{pat['avg_engagement_rate']}{DIM}, "
                  f"sources={RESET}{pat['source_video_count']}{DIM}){RESET}")
            print(f"      {DIM}template:{RESET} {YELLOW}{pat['template']}{RESET}")
            for ex in pat.get("examples", [])[:3]:
                print(f"        {DIM}- \"{ex}\"{RESET}")
            print()

        # ── AGENT 3 ────────────────────────────────────────────────────────
        step(3, "Script writer  (REAL Claude — Opus 4.7)")
        r3 = scriptwriter_run(account, {"slot": "scriptwriter"})
        scripts = r3.get("scripts", [])
        print(f"  {GREEN}✓{RESET} {len(scripts)} variants passed validation, "
              f"{r3.get('dropped_count', 0)} dropped")
        print()
        for i, v in enumerate(scripts):
            tier = f"{BOLD}{GREEN}VARIANT {i}{RESET}"
            print(f"  ┌─ {tier} ─ {DIM}video_id={v['video_id'][:10]}…{RESET}")
            print(f"  │  {DIM}product:{RESET}   {MAGENTA}{v.get('source_product_id')}{RESET}")
            print(f"  │  {DIM}pattern:{RESET}   {MAGENTA}{v.get('source_pattern_id')}{RESET}")
            print(f"  │  {DIM}duration:{RESET}  {v['target_duration_seconds']}s")
            print(f"  │")
            print(f"  │  {BOLD}HOOK (0-3s):{RESET}")
            print(f"  │    {YELLOW}\"{v['hook']}\"{RESET}")
            print(f"  │")
            print(f"  │  {BOLD}BODY BEATS:{RESET}")
            for beat in v.get("body_beats", []):
                t = beat.get("t", 0)
                label = beat.get("label", "")
                text = beat.get("text", "")
                print(f"  │    {DIM}[{t:>2}s · {label:<6}]{RESET} {text}")
            print(f"  │")
            print(f"  │  {BOLD}VOICEOVER (full):{RESET}")
            for line in _wrap(v["voiceover_text"], 64):
                print(f"  │    {line}")
            print(f"  │")
            print(f"  │  {BOLD}CAPTION:{RESET}  {v['caption']}")
            print(f"  │  {BOLD}HASHTAGS:{RESET} {' '.join(v.get('hashtags', []))}")
            print(f"  │")
            val = v.get("validation", {})
            if val.get("passed"):
                print(f"  │  {GREEN}✓ validation passed{RESET}  "
                      f"{DIM}({_estimate_words(v['voiceover_text'])} words "
                      f"≈ {_estimate_duration(v['voiceover_text']):.1f}s){RESET}")
            else:
                print(f"  │  {YELLOW}⚠ issues: {val.get('issues')}{RESET}")
            print(f"  └{'─' * 67}")
            print()

        # ── AGENT 4 ────────────────────────────────────────────────────────
        step(4, "Video prompt engineer  (deterministic — no Claude)")
        m4 = videoprompt_run(account, {"slot": "videoprompt"})
        print(f"  {GREEN}✓{RESET} formatted {m4.get('formatted_count', 0)} HeyGen payloads")
        print()
        primary = scripts[0]
        prompt_path = (Path("data/video_prompts") / "sharpguylab"
                       / today_str() / f"{primary['video_id']}.json")
        prompt_doc = json.loads(prompt_path.read_text())
        print(f"  {BOLD}HeyGen v2/video/generate body (variant 0):{RESET}")
        print()
        body_str = json.dumps(prompt_doc["payload"], indent=2)
        for line in body_str.split("\n"):
            print(f"  {DIM}│{RESET} {line}")
        print()
        sel = prompt_doc.get("selection", {})
        print(f"  {BOLD}selection details:{RESET}")
        print(f"    {DIM}avatar:{RESET}  "
              f"{MAGENTA}{sel.get('avatar', {}).get('avatar_id')}{RESET}  "
              f"{DIM}({sel.get('avatar', {}).get('vibe')}){RESET}")
        print(f"    {DIM}voice:{RESET}   "
              f"{MAGENTA}{sel.get('voice', {}).get('voice_id')}{RESET}  "
              f"{DIM}({sel.get('voice', {}).get('tone')}){RESET}")

        # ── AGENT 5 (skipped) ──────────────────────────────────────────────
        step(5, "Video generator  (SKIPPED — would call HeyGen)")
        print(f"  {YELLOW}skipped{RESET} — running this would burn HeyGen credits.")
        print(f"  the payload above is what would POST to /v2/video/generate.")

        # ── AGENT 6 (skipped) + final post preview ────────────────────────
        step(6, "Editor  (SKIPPED — would run ffmpeg)")
        print(f"  {YELLOW}skipped{RESET} — ffmpeg assembly not exercised.")
        print(f"  what the final TikTok post would look like:")
        print()

        final_hashtags, debug = finalize_hashtags(
            account=account, script_hashtags=primary["hashtags"],
        )
        print(f"  {BOLD}{GREEN}┌─ POST PREVIEW ({primary['video_id'][:10]}…) "
              + "─" * 22 + "┐" + RESET)
        print(f"  {GREEN}│{RESET} {BOLD}caption:{RESET}")
        for line in _wrap(primary["caption"], 60):
            print(f"  {GREEN}│{RESET}   {line}")
        print(f"  {GREEN}│{RESET}")
        print(f"  {GREEN}│{RESET} {BOLD}finalized hashtags:{RESET}")
        print(f"  {GREEN}│{RESET}   {' '.join(final_hashtags)}")
        print(f"  {GREEN}│{RESET}")
        print(f"  {GREEN}│{RESET} {BOLD}hashtag sources:{RESET}")
        for src, tags in debug.get("sources", {}).items():
            if tags:
                print(f"  {GREEN}│{RESET}   {DIM}{src}:{RESET} {' '.join(tags)}")
        print(f"  {GREEN}│{RESET}")
        print(f"  {GREEN}│{RESET} {BOLD}affiliate (would be):{RESET}")
        prod = primary.get("source_product_id", "?")
        print(f"  {GREEN}│{RESET}   bio:    {DIM}tiktok.com/view/product/{prod}?aff_id=…{RESET}")
        print(f"  {GREEN}│{RESET}   pinned: {DIM}🛒 link → …{RESET}")
        print(f"  {BOLD}{GREEN}└{'─' * 60}┘{RESET}")

        banner("✓ DRY RUN COMPLETE — verify above before HeyGen credit burn")
        print(f"  {DIM}artifacts written to data/{{trends,hooks,scripts,video_prompts}}/sharpguylab/{today_str()}/{RESET}")
        print()

    finally:
        for p in patches:
            p.stop()


def _wrap(text: str, width: int) -> list[str]:
    words = text.split()
    out: list[str] = []
    cur = ""
    for w in words:
        if len(cur) + len(w) + 1 > width:
            out.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).lstrip()
    if cur:
        out.append(cur)
    return out


def _estimate_words(text: str) -> int:
    return len(text.split())


def _estimate_duration(text: str, wpm: int = 150) -> float:
    return _estimate_words(text) / (wpm / 60)


if __name__ == "__main__":
    main()
