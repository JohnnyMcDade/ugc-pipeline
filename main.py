"""Master orchestrator. Loads config, registers all 8 agents against the
schedule for every enabled account, and runs forever.

Run:
    python main.py             # start scheduler
    python main.py run scout   # one-shot: run a single agent for all accounts now
    python main.py run scout sharpguylab  # one-shot: single agent, single account
"""

from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv

from agents.editor.editor import run as editor_run
from agents.health.monitor import run as health_monitor_run
from agents.health.report import run as health_report_run
from agents.hooks.analyzer import run as hooks_run
from agents.monitor.tracker import run as monitor_run
from agents.music_scout.scout import run as music_scout_run
from agents.publisher.publisher import run as publisher_run
from agents.scout.scout import run as scout_run
from agents.scriptwriter.writer import run as scriptwriter_run
from agents.videogen.generator import run as videogen_run
from agents.videoprompt.engineer import run as videoprompt_run
from core.config_loader import AccountConfig, PipelineConfig, load_pipeline
from core.logger import get_logger
from core.scheduler import Scheduler

CONFIG_ROOT = Path(__file__).parent / "config"


def _stub(name: str):
    def _fn(account: AccountConfig, ctx: dict) -> None:
        get_logger(name, account.handle).info(
            "stub agent invoked — not yet implemented",
            extra={"slot": ctx.get("slot")},
        )
    return _fn


# Map every scheduler slot → the agent function that handles it.
# Agents 2-8 are stubs until implemented.
AGENT_REGISTRY = {
    "scout":        ("scout",        scout_run),
    "hooks":        ("hooks",        hooks_run),
    "scriptwriter": ("scriptwriter", scriptwriter_run),
    "videoprompt":  ("videoprompt",  videoprompt_run),
    "videogen":     ("videogen",     videogen_run),
    "editor":       ("editor",       editor_run),
    "publisher_1":  ("publisher",    publisher_run),
    "publisher_2":  ("publisher",    publisher_run),
    "monitor":      ("monitor",      monitor_run),
    "music_scout":  ("music_scout",  music_scout_run),     # Agent 9 — weekly Sunday
}

# Agent 10 — global slots (not per-account). Different fn signature:
# (pipeline, ctx) instead of (account, ctx). Registered via register_global.
GLOBAL_AGENT_REGISTRY = {
    "health_check":  ("health_monitor", health_monitor_run),     # every 15 min
    "health_report": ("health_report",  health_report_run),      # daily 7 AM
}


def build_scheduler(pipeline: PipelineConfig) -> Scheduler:
    sched = Scheduler(pipeline)
    for slot, (agent_name, fn) in AGENT_REGISTRY.items():
        sched.register(slot, agent_name, fn)
    for slot, (agent_name, fn) in GLOBAL_AGENT_REGISTRY.items():
        sched.register_global(slot, agent_name, fn)
    return sched


def run_forever() -> None:
    # override=True so a shell-exported empty placeholder (e.g.
    # ANTHROPIC_API_KEY="") doesn't silently shadow the real value in .env.
    load_dotenv(override=True)
    pipeline = load_pipeline(CONFIG_ROOT)
    log = get_logger("main")
    log.info(
        "pipeline starting",
        extra={"accounts": [a.handle for a in pipeline.accounts]},
    )
    sched = build_scheduler(pipeline)
    sched.run_forever()


def run_once(slot: str, only_handle: str | None) -> None:
    # override=True so a shell-exported empty placeholder (e.g.
    # ANTHROPIC_API_KEY="") doesn't silently shadow the real value in .env.
    load_dotenv(override=True)
    pipeline = load_pipeline(CONFIG_ROOT)
    log = get_logger("main")

    # Global slots (Agent 10) take a different signature — (pipeline, ctx).
    if slot in GLOBAL_AGENT_REGISTRY:
        agent_name, fn = GLOBAL_AGENT_REGISTRY[slot]
        log.info("one-shot global run", extra={"slot": slot})
        try:
            fn(pipeline, {"slot": slot, "mode": "one_shot"})
        except Exception:
            get_logger(agent_name).exception("global agent crashed")
        return

    if slot not in AGENT_REGISTRY:
        valid = list(AGENT_REGISTRY) + list(GLOBAL_AGENT_REGISTRY)
        log.error("unknown slot", extra={"slot": slot, "valid": valid})
        sys.exit(2)

    agent_name, fn = AGENT_REGISTRY[slot]
    targets = (
        [pipeline.account(only_handle)] if only_handle else pipeline.accounts
    )
    log.info("one-shot run", extra={"slot": slot, "accounts": [a.handle for a in targets]})
    for account in targets:
        try:
            fn(account, {"slot": slot, "mode": "one_shot"})
        except Exception:
            get_logger(agent_name, account.handle).exception("agent crashed")


def main(argv: list[str]) -> None:
    if len(argv) >= 2 and argv[1] == "run":
        slot = argv[2] if len(argv) >= 3 else "scout"
        handle = argv[3] if len(argv) >= 4 else None
        run_once(slot, handle)
        return
    run_forever()


if __name__ == "__main__":
    main(sys.argv)
