"""Structured JSON logging, segregated per-account.

Usage:
    log = get_logger("scout", account="sharpguylab")
    log.info("scanned creative center", extra={"products_found": 42})
"""

from __future__ import annotations

import json
import logging
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

_LOG_ROOT = Path("data/logs")
_lock = threading.Lock()
_RESERVED_LOGRECORD_KEYS = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "message", "asctime",
}


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for k, v in record.__dict__.items():
            if k not in _RESERVED_LOGRECORD_KEYS and not k.startswith("_"):
                payload[k] = v
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


_configured: set[str] = set()


def get_logger(agent: str, account: str | None = None) -> logging.Logger:
    name = f"{agent}.{account}" if account else agent
    # Lock the configure step — APScheduler runs jobs in parallel threads, and
    # without this two threads can both pass the `name in _configured` check
    # and each add their own stream + file handler, producing duplicated log
    # lines for every subsequent call.
    with _lock:
        if name in _configured:
            return logging.getLogger(name)
        logger = logging.getLogger(name)
        logger.setLevel(logging.INFO)
        logger.propagate = False

        fmt = _JsonFormatter()

        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(fmt)
        logger.addHandler(stream)

        if account:
            log_dir = _LOG_ROOT / account
            log_dir.mkdir(parents=True, exist_ok=True)
            date_stamp = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
            file_handler = logging.FileHandler(log_dir / f"{date_stamp}.jsonl")
            file_handler.setFormatter(fmt)
            logger.addHandler(file_handler)

        _configured.add(name)
        return logger
