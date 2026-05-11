"""Wraps APScheduler. Registers every (agent, account) pair against a cron
expression read from master.yaml.

Each scheduled job is the agent function applied to one account. Agents that
operate globally (none currently) would be registered without an account loop.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from core.config_loader import AccountConfig, PipelineConfig
from core.logger import get_logger

AgentFn = Callable[[AccountConfig, dict[str, Any]], Any]

# Sized for: 3 accounts * up to 2 agents firing in adjacent slots, with
# headroom. Each scheduled (slot, account) pair has a unique job_id, so
# different accounts at the same slot run concurrently in different threads —
# this is how the pipeline parallelizes across @sharpguylab, @rideupgrades,
# @passivepoly. APScheduler's `max_instances=1` is per-job-id, so it doesn't
# block cross-account parallelism.
_DEFAULT_MAX_WORKERS = 16


class Scheduler:
    def __init__(self, pipeline: PipelineConfig, *, max_workers: int = _DEFAULT_MAX_WORKERS) -> None:
        self.pipeline = pipeline
        self.sched = BlockingScheduler(
            timezone=pipeline.master.timezone,
            executors={"default": ThreadPoolExecutor(max_workers=max_workers)},
        )
        self.log = get_logger("scheduler")

    def register(self, slot: str, agent_name: str, fn: AgentFn) -> None:
        """Register `fn` to run for every enabled account at the cron in
        master.schedule[slot]. `slot` may be the same as `agent_name` or a
        variant (e.g. publisher_1, publisher_2 both call the publisher fn).
        """
        cron_expr = self.pipeline.master.schedule.get(slot)
        if not cron_expr:
            self.log.warning("no schedule for slot, skipping", extra={"slot": slot})
            return
        trigger = CronTrigger.from_crontab(cron_expr, timezone=self.pipeline.master.timezone)

        for account in self.pipeline.accounts:
            self.sched.add_job(
                fn,
                trigger=trigger,
                args=[account, {"slot": slot}],
                id=f"{slot}:{account.handle}",
                name=f"{agent_name} for @{account.handle}",
                misfire_grace_time=600,
                coalesce=True,
                max_instances=1,
            )
            self.log.info(
                "registered job",
                extra={"slot": slot, "agent": agent_name, "account": account.handle, "cron": cron_expr},
            )

    def register_global(self, slot: str, agent_name: str, fn) -> None:
        """Like `register` but the job fires ONCE per cron tick (not per
        account). The function signature is (pipeline, ctx) instead of
        (account, ctx). Used for cross-account agents like the health
        monitor and daily report.
        """
        cron_expr = self.pipeline.master.schedule.get(slot)
        if not cron_expr:
            self.log.warning("no schedule for global slot, skipping", extra={"slot": slot})
            return
        trigger = CronTrigger.from_crontab(cron_expr, timezone=self.pipeline.master.timezone)
        self.sched.add_job(
            fn,
            trigger=trigger,
            args=[self.pipeline, {"slot": slot}],
            id=slot,
            name=f"{agent_name} (global)",
            misfire_grace_time=600,
            coalesce=True,
            max_instances=1,
        )
        self.log.info(
            "registered global job",
            extra={"slot": slot, "agent": agent_name, "cron": cron_expr},
        )

    def run_forever(self) -> None:
        self.log.info(
            "scheduler starting",
            extra={
                "jobs": len(self.sched.get_jobs()),
                "accounts": [a.handle for a in self.pipeline.accounts],
                "parallel_max_workers": _DEFAULT_MAX_WORKERS,
            },
        )
        self.sched.start()
