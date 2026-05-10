#!/usr/bin/env python3
"""Visual pipeline walkthrough for screen recordings.

Run: python demo.py

This is NOT the real pipeline. No API calls, no real videos, no TikTok posts.
It simulates one full slot for @sharpguylab so you can see what the
production scheduler does at each step. Real pipeline entry point: main.py.
"""

from __future__ import annotations

import itertools
import random
import sys
import time

# --- ANSI helpers -----------------------------------------------------------

RESET   = "\033[0m"
BOLD    = "\033[1m"
DIM     = "\033[2m"
CYAN    = "\033[36m"
GREEN   = "\033[32m"
YELLOW  = "\033[33m"
MAGENTA = "\033[35m"
BLUE    = "\033[34m"
GREY    = "\033[90m"


def cprint(text: str, *codes: str) -> None:
    print("".join(codes) + text + RESET)


def step(n: int, total: int, title: str) -> None:
    print()
    cprint(f"━━━ Step {n}/{total} ━━━ ", BOLD, CYAN)
    cprint(f"  {title}", BOLD)


def done(message: str) -> None:
    print(f"  {GREEN}✓{RESET} {message}")


def info(message: str) -> None:
    print(f"  {DIM}{message}{RESET}")


# --- Spinner ----------------------------------------------------------------

_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def spin(message: str, duration: float = 2.0, color: str = CYAN) -> None:
    """Show a spinner with `message` for `duration` seconds, then clear the line."""
    frames = itertools.cycle(_SPINNER_FRAMES)
    end = time.monotonic() + duration
    last_len = 0
    while time.monotonic() < end:
        line = f"  {color}{next(frames)}{RESET} {DIM}{message}{RESET}"
        sys.stdout.write("\r" + line)
        sys.stdout.flush()
        last_len = len(message) + 6
        time.sleep(0.08)
    # Clear the spinner line so the `done()` call writes on a clean line.
    sys.stdout.write("\r" + " " * (last_len + 2) + "\r")
    sys.stdout.flush()


def progress_bar(message: str, duration: float = 3.5, width: int = 30, color: str = BLUE) -> None:
    """Fill a progress bar over `duration` seconds."""
    steps = 40
    delay = duration / steps
    for i in range(steps + 1):
        pct = i / steps
        filled = int(width * pct)
        bar = "█" * filled + "░" * (width - filled)
        sys.stdout.write(f"\r  {color}{bar}{RESET} {int(pct * 100):3d}%  {DIM}{message}{RESET}")
        sys.stdout.flush()
        time.sleep(delay)
    print()


# --- Fixture data (so the demo feels real, not abstract) --------------------

TRENDING_PRODUCTS = [
    ("DateNight Cologne by Notum", "$32.50", "20%", 0.87),
    ("Sharp Edge Trimmer Kit",     "$39.99", "22%", 0.81),
    ("Velvetier Beard Oil",        "$24.99", "18%", 0.74),
]

HOOK_PATTERNS = [
    ("POV smell test",      "POV: she smelled my neck and asked what I was wearing", 0.142, 9),
    ("Contrarian opener",   "stop using your roommate's body wash",                  0.118, 7),
    ("Identity callout",    "if you're 22 and still using Axe, this is for you",    0.094, 5),
]

PRIMARY_VARIANT = {
    "product": "DateNight Cologne by Notum",
    "hook":    "POV: she smelled my neck and asked what I was wearing",
    "body":    ("this stuff is unreal. been wearing it two weeks and three different "
                "girls have asked. vanilla, sandalwood, just enough musk. link in bio."),
    "duration": 30,
}


def fake_video_id() -> str:
    return "v_" + "".join(random.choices("0123456789abcdef", k=10))


# --- Banner -----------------------------------------------------------------

def banner() -> None:
    line = "═" * 62
    print()
    cprint(line, CYAN)
    cprint("  UGC PIPELINE — DEMO RUN", BOLD, CYAN)
    cprint("  simulating one full slot for @sharpguylab", DIM)
    cprint(line, CYAN)
    print()
    info("This walkthrough prints what each agent does — no real API")
    info("calls, no videos, no TikTok posts. See main.py for the real one.")


# --- The demo ---------------------------------------------------------------

def main() -> None:
    random.seed(42)
    banner()
    time.sleep(1.5)

    TOTAL = 7

    # --- Step 1 — Trend scout ---
    step(1, TOTAL, "Scanning TikTok trends for @sharpguylab...")
    info("agent: scout (08:00 slot)")
    spin("hitting Creative Center, ranking products by Claude scorer...", 2.0)
    done("scan complete")
    print()
    cprint("  trending products (ranked):", BOLD)
    for name, price, commission, score in TRENDING_PRODUCTS:
        print(f"    {GREEN}•{RESET} {name}  "
              f"{DIM}({price}, {commission} commission, score {MAGENTA}{score}{DIM}){RESET}")
    time.sleep(1.5)

    # --- Step 2 — Result write ---
    step(2, TOTAL, "Found 3 trending grooming products")
    info(f"top score: {MAGENTA}0.87{RESET}  ·  threshold: 0.55  ·  3/12 accepted")
    info("written to → data/trends/sharpguylab/2026-05-10/products.json")
    time.sleep(1.3)

    # --- Step 3 — Hook research ---
    step(3, TOTAL, "Analyzing top performing hook patterns...")
    info("agent: hooks (06:00 slot, parallel to scout)")
    spin("scraping reference creators + classifying 60 hooks via Claude...", 2.0)
    done("pattern extraction complete  ·  3 clusters above threshold")
    print()
    cprint("  top hook patterns:", BOLD)
    for name, example, er, n in HOOK_PATTERNS:
        print(f"    {GREEN}•{RESET} {BOLD}{name}{RESET}"
              f"  {DIM}— avg ER {MAGENTA}{er:.3f}{DIM}, {n} source videos{RESET}")
        print(f"      {YELLOW}\"{example}\"{RESET}")
    time.sleep(1.7)

    # --- Step 4 — Scriptwriter ---
    step(4, TOTAL, "Writing 3 script variants...")
    info("agent: scriptwriter (07:00 slot)")
    spin("Claude (Opus 4.7) — persona-locked, banned-phrase-filtered...", 2.0)
    done("3/3 variants passed local validation")
    print()
    v = PRIMARY_VARIANT
    cprint("  variant 0 (primary):", BOLD)
    print(f"    {DIM}product:{RESET}  {MAGENTA}{v['product']}{RESET}")
    print(f"    {DIM}hook:{RESET}     {YELLOW}\"{v['hook']}\"{RESET}")
    print(f"    {DIM}body:{RESET}     {v['body']}")
    print(f"    {DIM}duration:{RESET} {v['duration']}s  ·  {DIM}est word count:{RESET} 73")
    time.sleep(1.8)

    # --- Step 5 — Video generation ---
    step(5, TOTAL, "Generating HeyGen avatar video...")
    info("agent: videogen (08:05 slot)")
    print()
    print(f"    {DIM}avatar:{RESET}    {MAGENTA}Leszek_standing_outdoorcasual_front{RESET}")
    print(f"    {DIM}voice:{RESET}     {MAGENTA}Callahan{RESET}  {DIM}(3ea8b0a9…){RESET}")
    print(f"    {DIM}dimension:{RESET} 1080×1920  ·  {DIM}aspect:{RESET} 9:16")
    print()
    progress_bar("POST /v2/video/generate → polling status...", 3.5)
    done("HeyGen returned status: completed")
    vid = fake_video_id()
    info(f"video_id: {MAGENTA}{vid}{RESET}")
    info(f"file:     data/raw_videos/sharpguylab/2026-05-10/{vid}/heygen.mp4 (4.2 MB)")
    info(f"QC: audio ✓  ·  9:16 ✓  ·  duration 30.2s ✓  ·  no black frames ✓")
    time.sleep(1.4)

    # --- Step 6 — Editor ---
    step(6, TOTAL, "Video ready — assembling final 9:16 MP4...")
    info("agent: editor (10:00 slot)  ·  ffmpeg + Pillow")
    spin("generating ASS captions from voiceover_text (Hook + 8 Body cues)...", 1.6)
    done("captions burned in")
    spin("mixing music: mens_grooming/track_3.mp3 at -18dB (ducked under voice)...", 1.6)
    done("audio mix complete")
    spin("ffmpeg final encode → 1080×1920 / h264 / aac / 30fps / +faststart...", 1.6)
    done("final.mp4 written")
    info(f"file: data/final_videos/sharpguylab/2026-05-10/{vid}/final.mp4 (3.8 MB)")
    time.sleep(1.5)

    # --- Step 7 — Publisher ---
    step(7, TOTAL, "Ready to post to @sharpguylab on TikTok")
    info("agent: publisher_1 (12:00 ET slot)")
    print()
    box_top = "┌─ post preview " + "─" * 38 + "┐"
    box_bot = "└" + "─" * 53 + "┘"
    cprint(f"  {box_top}", BOLD, GREEN)
    print(f"  {GREEN}│{RESET}  {BOLD}caption:{RESET}")
    print(f"  {GREEN}│{RESET}    POV: she smelled my neck and asked what I was")
    print(f"  {GREEN}│{RESET}    wearing 🌹")
    print(f"  {GREEN}│{RESET}")
    print(f"  {GREEN}│{RESET}  {BOLD}hashtags:{RESET}")
    print(f"  {GREEN}│{RESET}    #mensgrooming #cologne #dating #fyp #smelltest")
    print(f"  {GREEN}│{RESET}")
    print(f"  {GREEN}│{RESET}  {BOLD}affiliate:{RESET}")
    print(f"  {GREEN}│{RESET}    bio:    {DIM}tiktok.com/view/product/…?aff_id=…{RESET}")
    print(f"  {GREEN}│{RESET}    pinned: {DIM}🛒 link → …{RESET}")
    cprint(f"  {box_bot}", BOLD, GREEN)
    print()
    done(f"would call: {DIM}TikTokPublishClient.post_video(final.mp4, caption){RESET}")
    done(f"would log:  {DIM}data/published_log/sharpguylab/2026-05-10/{vid}.json{RESET}")
    time.sleep(1.0)

    # --- Outro ---
    print()
    line = "═" * 62
    cprint(line, CYAN)
    cprint("  ✓ demo complete — full pipeline simulated end-to-end", BOLD, GREEN)
    cprint("  run `python main.py` to start the real scheduler.", DIM)
    cprint(line, CYAN)
    print()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n{YELLOW}demo aborted by user{RESET}")
