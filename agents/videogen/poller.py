"""Generic polling utility used by both video clients.

Treats provider status strings flexibly — `completed`, `succeeded`, `success`,
`done` all count as terminal-success; `failed`, `error`, `cancelled` as
terminal-failure. Anything else is treated as in-progress.

Usage:
    result = poll_until_complete(
        job_id="abc",
        status_fn=lambda jid: client.get_status(jid),
        interval_seconds=30,
        timeout_seconds=1200,
        log=logger,
    )
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

_TERMINAL_SUCCESS = {"completed", "succeeded", "success", "done", "finished", "ready"}
_TERMINAL_FAILURE = {"failed", "error", "errored", "cancelled", "canceled", "rejected"}


class PollerError(RuntimeError):
    pass


class PollerTimeout(PollerError):
    pass


class PollerJobFailed(PollerError):
    pass


def poll_until_complete(
    job_id: str,
    status_fn: Callable[[str], dict[str, Any]],
    *,
    interval_seconds: int = 30,
    timeout_seconds: int = 1200,
    log: logging.Logger | None = None,
    label: str = "job",
) -> dict[str, Any]:
    start = time.monotonic()
    last_status: str | None = None
    while True:
        elapsed = time.monotonic() - start
        if elapsed >= timeout_seconds:
            raise PollerTimeout(
                f"{label} {job_id!r} did not complete in {timeout_seconds}s "
                f"(last status: {last_status!r})"
            )

        result = status_fn(job_id)
        status = str(result.get("status", "")).lower()
        if status != last_status and log:
            log.info(
                "poll status",
                extra={"job_id": job_id, "label": label, "status": status, "elapsed_s": int(elapsed)},
            )
        last_status = status

        if status in _TERMINAL_SUCCESS:
            return result
        if status in _TERMINAL_FAILURE:
            raise PollerJobFailed(
                f"{label} {job_id!r} reported terminal failure: {result.get('error') or result}"
            )

        # Cap the final sleep so we don't overshoot the timeout.
        remaining = timeout_seconds - elapsed
        time.sleep(min(interval_seconds, max(1, remaining)))
