# TikTok UGC Pipeline — System Overview

Fully autonomous multi-account TikTok UGC video pipeline. Markets 3 accounts in parallel using an 8-agent architecture, one master orchestrator, and per-account YAML configs.

## Accounts

| Account | Niche | Style | Monetization | Cadence |
|---|---|---|---|---|
| `@sharpguylab` | Men's grooming | HeyGen AI avatar UGC | TikTok Shop affiliate (15–25%) | 2x/day |
| `@rideupgrades` | Car accessories | HeyGen AI avatar UGC | TikTok Shop affiliate (12–22%) | 2x/day |
| `@passivepoly` | Polymarket whale-tracker SaaS | HeyGen AI avatar UGC + Discord screenshot composites | Subscription via Whop ($9.99–$29.99/mo), CTA → passivepoly.com | 1–2x/day |

`@passivepoly` is a marketing surface for an existing product. The product backend (`JohnnyMcDade/polymarket-bot`, `launcher.py`, 6 agents on Railway) is **already built and live**. This pipeline only generates content — it pulls real win/loss stats from the existing backend via [integrations/passivepoly_backend.py](integrations/passivepoly_backend.py) and never modifies it.

## 8-Agent Pipeline

| # | Agent | Folder | Responsibility |
|---|---|---|---|
| 1 | Trend & product scout | [agents/scout/](agents/scout/) | Daily TikTok Creative Center scan + trending products (acct 1, 2). Pulls live PassivePoly stats (acct 3). |
| 2 | Hook researcher | [agents/hooks/](agents/hooks/) | Scrapes top videos per niche, extracts winning hook patterns. |
| 3 | Script writer | [agents/scriptwriter/](agents/scriptwriter/) | 3–5 script variants per account, persona-locked. |
| 4 | Video prompt engineer | [agents/videoprompt/](agents/videoprompt/) | Deterministic HeyGen v2/video/generate payload formatter. |
| 5 | Video generator | [agents/videogen/](agents/videogen/) | Calls HeyGen API, polls until ready, QC. |
| 6 | Editor | [agents/editor/](agents/editor/) | FFmpeg: 9:16 crop, captions, music, overlays. |
| 7 | Publisher | [agents/publisher/](agents/publisher/) | Posts to correct account at peak times w/ correct CTA. Attaches `music_id` from Agent 9 at upload. |
| 8 | Performance monitor | [agents/monitor/](agents/monitor/) | Tracks per-video analytics, feeds winners to Agent 2, kills losers. |
| 9 | Music catalog scout | [agents/music_scout/](agents/music_scout/) | Weekly (Sun 5 AM) — pulls TikTok commercial-music `music_id`s per account mood. NOT a downloader: publisher attaches IDs at upload time so videos get the trending-sound algorithmic boost. |
| 10 | Self-repair monitor | [agents/health/](agents/health/) | Every 15 min — runs all health checks, auto-repairs known failures with exponential backoff, alerts Discord when repair gives up. Daily 7 AM Discord report. |

## Personas (locked — do not drift)

- **sharpguylab**: casual grooming bro, 22–28, gym + dating energy, talks about smelling good, looking sharp on dates.
- **rideupgrades**: car enthusiast, 25–35, mod-shop-floor energy, "you NEED this in your car" framing.
- **passivepoly**: confident 19yo who built an AI money system, calm-flex tone, never hypes "get rich quick" — frames as "I built a system, here's what it caught today."

## Daily Schedule (America/New_York)

```
*/15   Agent 10           (health monitor + auto-repair)
05:00  Agent 9            (music scout — Sundays only)
06:00  Agent 1 + Agent 2  (scan trends + extract hooks)
07:00  Agent 10           (daily health report → Discord)
07:00  Agent 3            (write scripts)
08:00  Agent 4 + Agent 5  (generate video prompts + videos)
10:00  Agent 6            (edit + assemble)
12:00  Agent 7            (post slot 1)
18:00  Agent 7            (post slot 2)
23:00  Agent 8            (analyze performance, feed back)
```

## Architecture

```
main.py                    # orchestrator entry — loads all account configs, runs scheduler
config/master.yaml         # global defaults, schedule, model versions
config/accounts/*.yaml     # one per account — persona, APIs, monetization, keywords
core/                      # shared infra: config_loader, logger, scheduler
agents/<n>/                # each agent: pure function over (config, state) → state
integrations/              # external API clients (HeyGen, TikTok, Claude, Whop, PassivePoly)
data/                      # outputs, segregated per-account
```

### Adding a new account
1. Drop `config/accounts/<handle>.yaml` modeled after the existing three.
2. Restart `main.py`. **No code changes.**

The orchestrator iterates over every YAML in `config/accounts/` — agents receive an `AccountConfig` object and operate generically on it.

## Tech Stack
- Python 3.11+
- FFmpeg (system binary)
- Anthropic Claude API (script writing, hook analysis, scoring)
- **HeyGen API** (avatar UGC for all 3 accounts)
- TikTok Business API + Creative Center scraping
- APScheduler (cron-style scheduling)

## Environment

Secrets live in `.env` (see `.env.example`). Never commit. Per-account API keys are referenced by name in YAML and resolved at load time from env.

## Conventions

- All agents are idempotent; rerunning the same day's slot must not duplicate posts.
- All agent outputs are JSON written to `data/<stage>/<account>/<YYYY-MM-DD>/`.
- Every video has a stable `video_id` (uuid4) flowing through every stage so Agent 8 can join analytics back to the originating script/hook.
- Logs: structured JSON, per-account, in `data/logs/<account>/`.
- No agent ever writes to the PassivePoly backend repo. Read-only.

## Agent Outputs (the contracts between stages)

| Agent | Writes to | Schema (key fields) |
|---|---|---|
| 1 (scout) | `data/trends/<handle>/<date>/products.json` (affiliate) or `signals.json` (passivepoly) | `products[].score`, `products[].hook_angle` / `signals[].category`, `signals[].evidence` |
| 2 (hooks) | `data/hooks/<handle>/<date>/patterns.json` | `patterns[].template`, `patterns[].examples`, `patterns[].final_score` |
| 3 (scriptwriter) | `data/scripts/<handle>/<date>/scripts.json` | `scripts[].video_id` (uuid, flows through to Agent 8), `scripts[].voiceover_text`, `scripts[].source_product_id`/`source_signal_id`, `scripts[].source_pattern_id`, `scripts[].validation.passed` |
| 4 (videoprompt) | `data/video_prompts/<handle>/<date>/<video_id>.json` + `manifest.json` | `platform: "heygen"`, `payload` (HeyGen v2 generate body), `metadata.source_script_video_id` |
| 5 (videogen) | `data/raw_videos/<handle>/<date>/<video_id>/{heygen.mp4, result.json}` + `manifest.json` | `result.qc_passed`, `result.files[].path`, `result.evidence_screenshot_required`, `result.duration_seconds_total` |
| 6 (editor) | `data/final_videos/<handle>/<date>/<video_id>/{final.mp4, result.json}` + `manifest.json` | `result.final_path`, `result.metadata.caption`, `result.metadata.hashtags`, `result.metadata.cta_url` |
| 7 (publisher) | `data/published_log/<handle>/<date>/<video_id>.json` + `manifest.json` | `publish_status` (`PUBLISH_COMPLETE`/`FAILED`), `tiktok_post_ids[]`, `caption_final`, `hashtags_final`, `link_plan` |
| 8 (monitor) | `data/analytics/<handle>/<date>/{per_video,winners,losers,report}.json` + `report.md` + `data/analytics/<handle>/exclusions/{patterns,products}.json` (cumulative) + `data/analytics/_global/<date>/summary.{json,md}` | `winners[]` consumed by Agent 2 the next morning. `losers[]` and `exclusions/*` are advisory today (Agents 1/2 don't read them yet — wire when ready). |
| 9 (music_scout) | `data/music_catalog/<subdir>/manifest.json` (per account) + `data/music_catalog/music_log.json` (shared history) | `manifest.tracks[].music_id` consumed by Agent 7 at upload time. `music_log.json` drives LRU rotation so the same track isn't reused within 7 days. |
| 10 (health) | `data/health/<date>/<HHMM>.json` (15-min snapshots) | Audit trail only — not consumed by other agents. Discord webhook is the live surface. |

## Music library

The editor (Agent 6) reads music from `data/music/<subdir>/`. Each account YAML names its subdir under `editor.music_subdir`:

- `data/music/mens_grooming/` → @sharpguylab
- `data/music/car_culture/` → @rideupgrades
- `data/music/polymarket_lofi/` → @passivepoly

Drop `.mp3` / `.m4a` / `.wav` files there. The editor rotates by `variant_index` so the same script always picks the same track on a re-run. Empty/missing dir → no music, warning logged, video still ships.

## Fonts

Caption rendering uses `Inter` by default. Either install Inter system-wide (`brew install --cask font-inter`) or drop the OTFs at `data/assets/fonts/Inter-Bold.otf` and `Inter-Regular.otf` — the editor passes `fontsdir` to libass when that directory exists.

## Status

**All 10 agents are fully implemented.** The pipeline is feature-complete: Agent 1 → … → Agent 8 → (winners.json) → Agent 2 the next morning. The only writes against external systems still pending wire-up are:

- `integrations/tiktok_creative_center.py:_fetch_raw` — Creative Center trend scrape (Agent 1, affiliate accounts).
- `integrations/tiktok_scraper.py:_fetch` — top-videos-by-username/keyword scrape (Agent 2).
- `integrations/tiktok_analytics.py:_fetch_one` — per-video metrics (Agent 8).
- `integrations/tiktok_shop_affiliate.py:_fetch` — clicks/conversions/revenue (Agent 8, affiliate accounts).
- PassivePoly backend (separate repo `JohnnyMcDade/polymarket-bot`) — needs four GET endpoints added to `launcher.py`: `/api/alerts/today`, `/api/stats/win-loss`, `/api/whales/biggest`, `/api/markets/notable-resolution`.
- HeyGen avatar/voice IDs in all three `config/accounts/*.yaml` are placeholders. Hit `GET /v2/avatars` and `GET /v2/voices` (developers.heygen.com) and paste real IDs.

The Whop integration ([integrations/whop_client.py](integrations/whop_client.py)) is fully implemented against Whop's documented v5 API.

## System requirements

- Python 3.11+
- `ffmpeg` and `ffprobe` on PATH (Agents 5 + 6 require both)
- Inter font installed system-wide OR placed at `data/assets/fonts/Inter-{Bold,Regular}.otf` (Agent 6 caption rendering)
- Pillow (auto-installed via requirements.txt) for Agent 6's passivepoly evidence screenshot — gracefully degrades if missing
- TikTok Content Posting API access token per account (`TIKTOK_SESSION_<HANDLE>`); pinned-comment endpoints are best-effort and treated as advisory if your API tier doesn't expose them

## The feedback loop (closed)

```
                                          ┌───────────────────────────────┐
                                          │  Agent 8 (23:00)              │
                                          │  → data/analytics/.../        │
                                          │     winners.json              │
                                          └──────────┬────────────────────┘
                                                     │ next morning
                                                     ▼
  Agent 1 (06:00) ──── trends ──┐         Agent 2 (06:00, parallel)
  Agent 2 (06:00) ──── hooks ───┼─── reads `yesterday/winners.json`,
                                │     upweights echoing patterns
  Agent 3 (07:00) ──── scripts (with `echoes_yesterday_winner: true` boost)
  Agent 4 (08:00) ──── prompts
  Agent 5 (08:05) ──── raw mp4s
  Agent 6 (10:00) ──── final mp4s
  Agent 7 (12:00, 18:00) ──── posts; logs to published_log
                                │
                                └─── Agent 7's hashtag_gen also reads
                                     `winners.json` to lift winning hashtags.
```
