from __future__ import annotations

import contextvars
import json
import logging
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any


logger = logging.getLogger("uvicorn.error")


class ScoutTimeoutError(TimeoutError):
    pass


SAFE_TIMEOUT_REPLY = (
    "I’m sorry, this request is taking longer than I can safely verify right now. "
    "Please try again in a moment."
)


@dataclass
class StageTiming:
    name: str
    elapsed_ms: float
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class RequestDiagnostics:
    request_id: str = field(default_factory=lambda: f"diag_{uuid.uuid4().hex}")
    started_at: float = field(default_factory=time.perf_counter)
    timings: list[StageTiming] = field(default_factory=list)
    model_calls: int = 0
    tool_calls: int = 0
    selected_specialists: list[str] = field(default_factory=list)
    correction_ran: bool = False
    timed_out: bool = False

    def record(self, name: str, elapsed_ms: float, **metadata: Any) -> None:
        clean_metadata = {
            key: value
            for key, value in metadata.items()
            if value is not None and isinstance(value, (str, int, float, bool))
        }
        self.timings.append(StageTiming(name=name, elapsed_ms=elapsed_ms, metadata=clean_metadata))
        _log("stage", self, stage=name, elapsed_ms=round(elapsed_ms, 2), **clean_metadata)

    def add_specialist(self, name: str) -> None:
        if name and name not in self.selected_specialists:
            self.selected_specialists.append(name)
            _log("specialist_selected", self, specialist=name)

    def summary(self) -> dict[str, Any]:
        elapsed_ms = (time.perf_counter() - self.started_at) * 1000
        slowest = max(self.timings, key=lambda timing: timing.elapsed_ms, default=None)
        return {
            "request_id": self.request_id,
            "elapsed_ms": round(elapsed_ms, 2),
            "model_calls": self.model_calls,
            "tool_calls": self.tool_calls,
            "selected_specialists": list(self.selected_specialists),
            "correction_ran": self.correction_ran,
            "timed_out": self.timed_out,
            "slowest_stage": slowest.name if slowest else None,
            "slowest_stage_ms": round(slowest.elapsed_ms, 2) if slowest else None,
        }


_current_diagnostics: contextvars.ContextVar[RequestDiagnostics | None] = contextvars.ContextVar(
    "scout_request_diagnostics",
    default=None,
)


def start_diagnostics() -> RequestDiagnostics:
    diagnostics = RequestDiagnostics()
    _current_diagnostics.set(diagnostics)
    _log("request_started", diagnostics)
    return diagnostics


def get_diagnostics() -> RequestDiagnostics | None:
    return _current_diagnostics.get()


def clear_diagnostics() -> None:
    _current_diagnostics.set(None)


def mark_timeout() -> None:
    diagnostics = get_diagnostics()
    if diagnostics is not None:
        diagnostics.timed_out = True
        _log("timeout", diagnostics)


def record_model_call(*, stage: str, elapsed_ms: float, message_count: int = 0, input_chars: int = 0) -> None:
    diagnostics = get_diagnostics()
    if diagnostics is None:
        return
    diagnostics.model_calls += 1
    diagnostics.record(
        stage,
        elapsed_ms,
        model_call=diagnostics.model_calls,
        message_count=message_count,
        input_chars=input_chars,
    )


def record_tool_call_timing(*, tool_name: str, elapsed_ms: float, agent_name: str | None = None) -> None:
    diagnostics = get_diagnostics()
    if diagnostics is None:
        return
    diagnostics.tool_calls += 1
    diagnostics.record(
        "tool_call",
        elapsed_ms,
        tool_name=tool_name,
        agent_name=agent_name or "unknown",
        tool_call=diagnostics.tool_calls,
    )


def record_selected_specialist(name: str) -> None:
    diagnostics = get_diagnostics()
    if diagnostics is not None:
        diagnostics.add_specialist(name)


def mark_correction_ran() -> None:
    diagnostics = get_diagnostics()
    if diagnostics is not None:
        diagnostics.correction_ran = True
        _log("correction_started", diagnostics)


@contextmanager
def timed_stage(name: str, **metadata: Any):
    started = time.perf_counter()
    try:
        yield
    finally:
        diagnostics = get_diagnostics()
        if diagnostics is not None:
            diagnostics.record(name, (time.perf_counter() - started) * 1000, **metadata)


def input_size(messages: list) -> tuple[int, int]:
    count = len(messages)
    chars = 0
    for message in messages:
        if isinstance(message, dict):
            chars += len(str(message.get("content", "")))
        else:
            chars += len(str(getattr(message, "content", "")))
    return count, chars


def log_request_complete() -> None:
    diagnostics = get_diagnostics()
    if diagnostics is not None:
        _log("request_complete", diagnostics, **diagnostics.summary())


def _log(event: str, diagnostics: RequestDiagnostics, **fields: Any) -> None:
    payload = {"event": event, "request_id": diagnostics.request_id, **fields}
    logger.warning("scout_diagnostics %s", json.dumps(payload, sort_keys=True))
