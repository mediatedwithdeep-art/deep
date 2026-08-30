"""Structured logging.

JSON in production so logs are queryable; human-readable in development.
Every record carries service, and a request/trace id when one is bound --
correlating a slow API call with the camera worker that caused it is
impossible otherwise.
"""

from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from contextvars import ContextVar
from typing import Any

_trace_id: ContextVar[str | None] = ContextVar("trace_id", default=None)
_camera_id: ContextVar[str | None] = ContextVar("camera_id", default=None)

_RESERVED = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "taskName", "message", "asctime",
}


def set_trace_id(value: str | None = None) -> str:
    tid = value or uuid.uuid4().hex[:16]
    _trace_id.set(tid)
    return tid


def get_trace_id() -> str | None:
    return _trace_id.get()


def set_camera_id(value: str | None) -> None:
    _camera_id.set(value)


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str):
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
                  + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "service": self.service,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if tid := _trace_id.get():
            payload["trace_id"] = tid
        if cid := _camera_id.get():
            payload["camera_id"] = cid
        for k, v in record.__dict__.items():
            if k not in _RESERVED and not k.startswith("_"):
                payload[k] = v
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class ConsoleFormatter(logging.Formatter):
    COLOURS = {"DEBUG": "\033[36m", "INFO": "\033[32m", "WARNING": "\033[33m",
               "ERROR": "\033[31m", "CRITICAL": "\033[35m"}
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        c = self.COLOURS.get(record.levelname, "")
        ts = time.strftime("%H:%M:%S", time.gmtime(record.created))
        extra = " ".join(f"{k}={v}" for k, v in record.__dict__.items()
                         if k not in _RESERVED and not k.startswith("_"))
        cid = _camera_id.get()
        prefix = f"[{cid}] " if cid else ""
        line = f"{ts} {c}{record.levelname:<8}{self.RESET} {record.name:<28} {prefix}{record.getMessage()}"
        if extra:
            line += f"  \033[90m{extra}{self.RESET}"
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def configure_logging(service: str, level: str = "INFO", fmt: str = "json") -> None:
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service) if fmt == "json" else ConsoleFormatter())
    root.addHandler(handler)
    root.setLevel(level.upper())
    # These are chatty and rarely useful at INFO.
    for noisy in ("uvicorn.access", "aiokafka", "urllib3", "asyncio", "multipart"):
        logging.getLogger(noisy).setLevel("WARNING")


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
