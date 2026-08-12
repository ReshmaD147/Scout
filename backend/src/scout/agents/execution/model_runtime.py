from __future__ import annotations

import asyncio
import time

from scout.agents.diagnostics import (
    get_diagnostics,
    input_size,
    mark_timeout,
    record_model_call,
    record_selected_specialist,
)
from scout.agents.evidence import get_evidence_entries
from scout.agents.execution.tool_first import _continue_after_tool_evidence
from scout.agents.intent_splitter import StructuredIntent
from scout.agents.orchestration.constants import SPECIALIST_NAMES
from scout.agents.verification_flow.completion import can_finalize_from_evidence


async def _wait_for(coro, timeout_seconds: float, stage: str):
    try:
        return await asyncio.wait_for(coro, timeout=timeout_seconds)
    except asyncio.TimeoutError as exc:
        mark_timeout()
        raise TimeoutError(f"{stage} timed out") from exc


class ProviderWorkflowError(RuntimeError):
    pass


def _is_provider_or_transport_error(exc: BaseException) -> bool:
    module_name = exc.__class__.__module__.lower()
    class_name = exc.__class__.__name__.lower()
    return (
        isinstance(exc, (TimeoutError, asyncio.TimeoutError))
        or "httpx" in module_name
        or "httpcore" in module_name
        or "ollama" in module_name
        or "readtimeout" in class_name
        or "connecterror" in class_name
        or "connection" in class_name
    )


def _record_handled_provider_error(exc: BaseException) -> None:
    diagnostics = get_diagnostics()
    if diagnostics is not None:
        diagnostics.record(
            "handled_provider_error",
            0,
            handled_provider_error_code=exc.__class__.__name__,
        )


def _result_contains_messages(value) -> bool:
    if isinstance(value, dict):
        if isinstance(value.get("messages"), list):
            return True
        return any(_result_contains_messages(item) for item in value.values())
    return isinstance(getattr(value, "messages", None), list)


async def _invoke_graph_with_timing(app, payload: dict, *, config: dict):
    if not hasattr(app, "astream_events"):
        messages = payload.get("messages", [])
        count, chars = input_size(messages)
        started = time.perf_counter()
        try:
            result = await app.ainvoke(payload, config=config)
        except Exception as exc:
            if _is_provider_or_transport_error(exc):
                _record_handled_provider_error(exc)
                raise ProviderWorkflowError("provider failure") from exc
            raise
        record_model_call(
            stage="supervisor_model_invocation",
            elapsed_ms=(time.perf_counter() - started) * 1000,
            message_count=count,
            input_chars=chars,
        )
        return result

    final_result = None
    model_starts: dict[str, tuple[float, int, int, str]] = {}
    try:
        stream = app.astream_events(payload, version="v2", config=config)
        async for event in stream:
            event_type = event.get("event")
            node_name = event.get("metadata", {}).get("langgraph_node", "")
            run_id = str(event.get("run_id", ""))

            if event_type == "on_chain_start" and node_name in SPECIALIST_NAMES:
                record_selected_specialist(node_name)

            if event_type == "on_chat_model_start":
                messages = event.get("data", {}).get("input", {}).get("messages", [])
                count, chars = input_size(messages if isinstance(messages, list) else [])
                model_starts[run_id] = (time.perf_counter(), count, chars, node_name or "unknown")

            if event_type == "on_chat_model_end" and run_id in model_starts:
                started, count, chars, node = model_starts.pop(run_id)
                stage = "specialist_model_invocation" if node in SPECIALIST_NAMES else "supervisor_model_invocation"
                record_model_call(
                    stage=stage,
                    elapsed_ms=(time.perf_counter() - started) * 1000,
                    message_count=count,
                    input_chars=chars,
                )

            if event_type == "on_chain_end":
                output = event.get("data", {}).get("output")
                if event.get("name") == "LangGraph" or _result_contains_messages(output):
                    final_result = output
    except Exception as exc:
        if _is_provider_or_transport_error(exc):
            _record_handled_provider_error(exc)
            raise ProviderWorkflowError("provider failure") from exc
        raise

    return final_result


async def _invoke_agent_with_timing(
    agent,
    payload: dict,
    *,
    config: dict,
    agent_name: str,
    structured_intent: StructuredIntent | None = None,
):
    if not hasattr(agent, "astream_events"):
        messages = payload.get("messages", [])
        count, chars = input_size(messages)
        started = time.perf_counter()
        try:
            result = await agent.ainvoke(payload, config=config)
        except Exception as exc:
            if _is_provider_or_transport_error(exc):
                _record_handled_provider_error(exc)
                raise ProviderWorkflowError("provider failure") from exc
            raise
        record_model_call(
            stage=agent_name,
            elapsed_ms=(time.perf_counter() - started) * 1000,
            message_count=count,
            input_chars=chars,
        )
        return result

    final_result = None
    model_starts: dict[str, tuple[float, int, int]] = {}
    record_selected_specialist(agent_name)
    early_completed = False
    stream = agent.astream_events(payload, version="v2", config=config)
    try:
        async for event in stream:
            event_type = event.get("event")
            run_id = str(event.get("run_id", ""))

            if event_type == "on_chat_model_start":
                messages = event.get("data", {}).get("input", {}).get("messages", [])
                count, chars = input_size(messages if isinstance(messages, list) else [])
                model_starts[run_id] = (time.perf_counter(), count, chars)

            if event_type == "on_chat_model_end" and run_id in model_starts:
                started, count, chars = model_starts.pop(run_id)
                record_model_call(
                    stage=agent_name,
                    elapsed_ms=(time.perf_counter() - started) * 1000,
                    message_count=count,
                    input_chars=chars,
                )

            if event_type == "on_chain_end":
                output = event.get("data", {}).get("output")
                if event.get("name") == "LangGraph" or _result_contains_messages(output):
                    final_result = output

            if event_type == "on_tool_end":
                if await _continue_after_tool_evidence(
                    structured_intent=structured_intent,
                    agent_name=agent_name,
                ):
                    final_result = {"messages": []}
                    early_completed = True
                    break

            if event_type in {"on_tool_end", "on_chain_end"}:
                decision = can_finalize_from_evidence(
                    intent=structured_intent.request_type if structured_intent else "",
                    structured_intent=structured_intent,
                    evidence_entries=get_evidence_entries(),
                    tool_history=[],
                )
                if decision.complete:
                    diagnostics = get_diagnostics()
                    if diagnostics is not None:
                        diagnostics.record(
                            "evidence_early_completion",
                            0,
                            evidence_early_completion_used=True,
                            evidence_completion_domain=decision.responsible_domain,
                            evidence_completion_reason=decision.completion_reason,
                            model_calls_avoided=1,
                        )
                    final_result = {"messages": []}
                    early_completed = True
                    break
    except Exception as exc:
        if _is_provider_or_transport_error(exc):
            _record_handled_provider_error(exc)
            raise ProviderWorkflowError("provider failure") from exc
        raise
    finally:
        if early_completed and hasattr(stream, "aclose"):
            await stream.aclose()
            diagnostics = get_diagnostics()
            if diagnostics is not None:
                diagnostics.record(
                    "specialist_stream_cancelled",
                    0,
                    specialist_stream_cancelled=True,
                    cancellation_cleanup_completed=True,
                )

    return final_result
