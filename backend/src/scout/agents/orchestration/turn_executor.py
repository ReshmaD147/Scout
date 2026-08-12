from __future__ import annotations

import re
import sys
import time
from typing import AsyncIterator

from scout.config import settings
from scout.agents.diagnostics import (
    get_diagnostics,
    input_size,
    record_model_call,
    record_selected_specialist,
    timed_stage,
)
from scout.agents.evidence import (
    clear_evidence_context,
    get_evidence_entries,
    get_tool_call_records,
    make_sub_intent_id,
    start_evidence_context,
)
from scout.agents.execution import (
    _inventory_constraints_from_structured_intent,
    _run_tool_first_if_possible,
)
from scout.agents.execution.model_runtime import (
    ProviderWorkflowError,
    _invoke_agent_with_timing,
    _invoke_graph_with_timing,
)
from scout.agents.intent_splitter import StructuredIntent
from scout.agents.result_normalization import (
    evidence_entries_from_tool_messages,
    extract_product_candidates,
    normalize_agent_result,
)
from scout.agents.orchestration.constants import HANDOFF_MARKERS, SPECIALIST_NAMES
from scout.agents.orchestration.turn_context import TurnExecutionContext
from scout.agents.routing import _get_specialist_registry
from scout.agents.tool_guard import (
    clear_inventory_argument_constraints,
    reset_guard,
    set_inventory_argument_constraints,
)
from scout.agents.verification_flow import (
    FinalizedResponse,
    _attempt_targeted_correction,
    _finalize_verified_response,
)


MAX_HISTORY_MESSAGES = 12


def _supervisor_override(name: str, current):
    supervisor_module = sys.modules.get("scout.agents.supervisor")
    override = getattr(supervisor_module, name, None) if supervisor_module is not None else None
    if override is not None and override is not current:
        return override
    return None


def _trim_history_for_model(history: list[dict]) -> list[dict]:
    if len(history) <= MAX_HISTORY_MESSAGES:
        return history
    return history[-MAX_HISTORY_MESSAGES:]


def _result_contains_messages(value) -> bool:
    if isinstance(value, dict):
        if isinstance(value.get("messages"), list):
            return True
        return any(_result_contains_messages(item) for item in value.values())
    return isinstance(getattr(value, "messages", None), list)


def extract_text(content) -> str:
    """Normalizes message content to a plain string."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return str(content)


def get_final_reply(messages: list) -> str:
    """Returns the last substantive specialist response."""
    any_specialist_responded = any(
        getattr(msg, "name", None) in SPECIALIST_NAMES for msg in messages
    )

    for msg in reversed(messages):
        name = getattr(msg, "name", None)
        if name not in SPECIALIST_NAMES:
            continue
        tool_calls = getattr(msg, "tool_calls", None)
        if tool_calls:
            continue
        text = extract_text(getattr(msg, "content", ""))
        stripped = text.strip()
        if not stripped or stripped in HANDOFF_MARKERS:
            continue
        if stripped.startswith("NEEDS_EXTERNAL_CHECK"):
            continue
        return text

    if not any_specialist_responded:
        for msg in reversed(messages):
            if getattr(msg, "name", None) == "supervisor":
                tool_calls = getattr(msg, "tool_calls", None)
                if tool_calls:
                    continue
                text = extract_text(getattr(msg, "content", ""))
                stripped = text.strip()
                if stripped and stripped not in HANDOFF_MARKERS:
                    return text

    return "I couldn't quite find what you're looking for — want to try asking a different way, or is there something else I can help with?"


async def _run_direct_specialist_turn(
    app,
    history: list[dict],
    messages_before: int,
    sub_intent: str,
    specialist_name: str,
    structured_intent: StructuredIntent | None = None,
):
    specialists = _get_specialist_registry(app)
    specialist = specialists[specialist_name]
    diagnostics = get_diagnostics()
    if diagnostics is not None:
        diagnostics.record(
            "direct_routing",
            0,
            direct_route_used=True,
            selected_specialist=specialist_name,
            supervisor_invoked=False,
        )

    result = await _invoke_agent_with_timing(
        specialist,
        {"messages": _trim_history_for_model(history)},
        config={"recursion_limit": 15},
        agent_name=specialist_name,
        structured_intent=structured_intent,
    )
    normalized = normalize_agent_result(
        result=result,
        input_message_count=messages_before,
        execution_mode="direct",
    )
    new_messages = normalized.messages

    if specialist_name == "recommend_agent" and normalized.routing_marker_present:
        external_agent = specialists.get("external_offer_agent")
        if external_agent is not None:
            diagnostics = get_diagnostics()
            if diagnostics is not None:
                diagnostics.record("external_fallback", 0, external_fallback_used=True)
            reset_guard(max_total_calls=10, max_identical_calls=1)
            external_result = await _invoke_agent_with_timing(
                external_agent,
                {"messages": [{"role": "user", "content": sub_intent}]},
                config={"recursion_limit": 15},
                agent_name="external_offer_agent",
                structured_intent=StructuredIntent(
                    text=sub_intent,
                    request_type="external_offer",
                    confidence=0.8,
                ),
            )
            external_normalized = normalize_agent_result(
                result=external_result,
                input_message_count=0,
                execution_mode="direct",
            )
            return external_normalized.messages, True

    return new_messages, False


def _alternatives_no_match(evidence_entries: list) -> bool:
    for entry in evidence_entries:
        if getattr(entry, "tool_name", None) != "alternatives" or not getattr(entry, "success", False):
            continue
        facts = getattr(entry, "normalized_facts", {})
        if isinstance(facts, dict) and facts.get("match_count") == 0:
            return True
    return False


def _similar_products_no_match_reply(structured_intent: StructuredIntent) -> str:
    product_name = "that item"
    match = re.search(r"\(([^)]+)\)", structured_intent.text or "")
    if match and match.group(1).strip():
        product_name = match.group(1).strip()
    constraints = []
    if structured_intent.color:
        constraints.append(str(structured_intent.color).strip())
    if structured_intent.size:
        constraints.append(f"size {structured_intent.size}")
    if structured_intent.budget_max is not None:
        constraints.append(f"under ${structured_intent.budget_max:g}")
    constraint_text = f" matching {', '.join(constraints)}" if constraints else ""
    return f"I couldn’t find a Scout alternative to {product_name}{constraint_text}."


async def _run_supervisor_turn_streaming(app, history: list[dict], messages_before: int) -> tuple[list, bool] | None:
    final_result = None
    seen_nodes: set[str] = set()
    model_starts: dict[str, tuple[float, int, int, str]] = {}

    async for event in app.astream_events(
        {"messages": _trim_history_for_model(history)}, version="v2", config={"recursion_limit": 15}
    ):
        node_name = event.get("metadata", {}).get("langgraph_node", "")
        ev_type = event.get("event")
        run_id = str(event.get("run_id", ""))

        if ev_type == "on_chain_start" and node_name and node_name not in seen_nodes:
            seen_nodes.add(node_name)
            if node_name in SPECIALIST_NAMES:
                record_selected_specialist(node_name)
            if node_name in ("recommend_agent", "external_offer_agent"):
                yield ("progress", "searching_products")
            elif node_name == "inventory_agent":
                yield ("progress", "checking_inventory")

        if ev_type == "on_chat_model_start":
            messages = event.get("data", {}).get("input", {}).get("messages", [])
            count, chars = input_size(messages if isinstance(messages, list) else [])
            model_starts[run_id] = (time.perf_counter(), count, chars, node_name or "unknown")

        if ev_type == "on_chat_model_end" and run_id in model_starts:
            started, count, chars, node = model_starts.pop(run_id)
            stage = "specialist_model_invocation" if node in SPECIALIST_NAMES else "supervisor_model_invocation"
            record_model_call(
                stage=stage,
                elapsed_ms=(time.perf_counter() - started) * 1000,
                message_count=count,
                input_chars=chars,
            )

        if ev_type == "on_chain_end":
            output = event.get("data", {}).get("output")
            if event.get("name") == "LangGraph" or _result_contains_messages(output):
                final_result = output

    if final_result is None:
        yield ("result", None)
        return

    normalized = normalize_agent_result(
        result=final_result,
        input_message_count=messages_before,
        execution_mode="supervisor",
    )
    yield ("result", (normalized.messages, normalized.routing_marker_present))


async def execute_single_intent_turn(
    app,
    history: list[dict],
    sub_intent: str,
    debug: bool = False,
    conversation_context: dict | None = None,
    *,
    stream_progress: bool = False,
    turn_context: TurnExecutionContext | None = None,
) -> AsyncIterator[tuple[str, object]]:
    if not sub_intent or not sub_intent.strip():
        yield ("result", ("I didn't quite catch that — could you rephrase your question?", []))
        return

    sub_intent_started = time.monotonic()
    correction_deadline = sub_intent_started + settings.SUB_INTENT_TIMEOUT_SECONDS
    sub_intent_id = make_sub_intent_id()
    start_evidence_context(sub_intent_id, attempt_number=0)
    try:
        reset_guard(max_total_calls=10, max_identical_calls=1)
        messages_before = len(history)
        history.append({"role": "user", "content": sub_intent})

        if stream_progress:
            yield ("progress", "understanding_request")

        turn_context = turn_context or TurnExecutionContext()
        direct_specialist = turn_context.direct_specialist
        structured_intent = turn_context.structured_intent
        if direct_specialist:
            set_inventory_argument_constraints(
                _inventory_constraints_from_structured_intent(structured_intent)
            )
            if stream_progress:
                if direct_specialist in ("recommend_agent", "external_offer_agent"):
                    yield ("progress", "searching_products")
                elif direct_specialist == "inventory_agent":
                    yield ("progress", "checking_inventory")
            if await _run_tool_first_if_possible(
                structured_intent=structured_intent,
                sub_intent=sub_intent,
                agent_name=direct_specialist,
            ):
                new_messages = []
                had_external_handoff = direct_specialist == "external_offer_agent"
            else:
                with timed_stage("direct_specialist_invocation", selected_specialist=direct_specialist):
                    new_messages, had_external_handoff = await _run_direct_specialist_turn(
                        app, history, messages_before, sub_intent, direct_specialist, structured_intent
                    )
        else:
            diagnostics = get_diagnostics()
            if diagnostics is not None:
                diagnostics.record("direct_routing", 0, direct_route_used=False, supervisor_invoked=True)
            if stream_progress:
                final_turn = None
                async for kind, payload in _run_supervisor_turn_streaming(app, history, messages_before):
                    if kind == "progress":
                        yield ("progress", payload)
                    else:
                        final_turn = payload
                if final_turn is None:
                    yield ("result", ("Something went wrong processing that — could you try again?", []))
                    return
                new_messages, had_external_handoff = final_turn
            else:
                graph_input = {"messages": _trim_history_for_model(history)}
                with timed_stage("supervisor_graph_invocation"):
                    result = await _invoke_graph_with_timing(app, graph_input, config={"recursion_limit": 15})
                if result is None:
                    yield ("result", ("Something went wrong processing that — could you try again?", []))
                    return
                normalized = normalize_agent_result(
                    result=result,
                    input_message_count=messages_before,
                    execution_mode="supervisor",
                )
                new_messages = normalized.messages
                had_external_handoff = normalized.routing_marker_present

        if debug:
            print(f"\n--- FULL MESSAGE TRACE (sub-intent: {sub_intent!r}) ---")
            for i, msg in enumerate(new_messages):
                role = getattr(msg, "type", "?")
                name = getattr(msg, "name", None)
                tool_calls = getattr(msg, "tool_calls", None)
                text = extract_text(getattr(msg, "content", ""))
                print(f"[{i}] role={role} name={name}")
                if tool_calls:
                    print(f"    tool_calls={tool_calls}")
                if text:
                    print(f"    content={text[:300]!r}")
            print("--- END TRACE ---\n")

        reply_text = get_final_reply(new_messages)
        evidence_entries = get_evidence_entries()
        if not evidence_entries:
            evidence_entries = evidence_entries_from_tool_messages(
                normalized_messages=new_messages,
                sub_intent_id=sub_intent_id,
                attempt_number=0,
            )
        all_products = extract_product_candidates(
            normalized_messages=new_messages,
            evidence_entries=evidence_entries,
        )
        products = (
            [p for p in all_products if p.get("source") == "external"]
            if had_external_handoff
            else all_products
        )

        if stream_progress:
            yield ("progress", "verifying_claims")
            yield ("progress", "preparing_response")

        finalize = _supervisor_override("_finalize_verified_response", _finalize_verified_response) or _finalize_verified_response
        finalized = finalize(
            original_reply=reply_text,
            products=products,
            evidence_entries=evidence_entries,
            customer_message=sub_intent,
        )
        attempt_correction = (
            _supervisor_override("_attempt_targeted_correction", _attempt_targeted_correction)
            or _attempt_targeted_correction
        )
        finalized = await attempt_correction(
            app,
            initial=finalized,
            customer_message=sub_intent,
            correction_attempted=False,
            sub_intent_id=sub_intent_id,
            correction_deadline=correction_deadline,
        )
        if (
            structured_intent
            and structured_intent.request_type == "similar_products"
            and not finalized.products
            and _alternatives_no_match(evidence_entries)
        ):
            finalized = FinalizedResponse(
                reply=_similar_products_no_match_reply(structured_intent),
                products=[],
                proposed_claims=finalized.proposed_claims,
                verification_result=finalized.verification_result,
            )
        get_records = _supervisor_override("get_tool_call_records", get_tool_call_records) or get_tool_call_records
        get_records()
        from scout.agents.context import _update_context_from_verified_turn

        _update_context_from_verified_turn(
            conversation_context,
            finalized=finalized,
            structured_intent=structured_intent,
        )
        history.append({"role": "assistant", "content": finalized.reply})
        yield ("result", (finalized.reply, finalized.products))
    finally:
        clear_inventory_argument_constraints()
        clear_evidence_context()
