from __future__ import annotations

import asyncio
import inspect
import json
import re
import time
from decimal import Decimal, InvalidOperation

from scout.config import settings
from scout.db.session import SessionLocal
from scout.services.product_service import add_to_cart_service
from scout.agents.diagnostics import (
    SAFE_TIMEOUT_REPLY,
    clear_diagnostics,
    get_diagnostics,
    input_size,
    log_request_complete,
    mark_timeout,
    mark_correction_ran,
    record_model_call,
    record_selected_specialist,
    record_tool_call_timing,
    start_diagnostics,
    timed_stage,
)
from scout.agents.model_provider import get_chat_model
from scout.agents.orchestration.graph_factory import (
    SUPERVISOR_PROMPT,
    build_supervisor_app,
)
from scout.agents.intent_splitter import (
    ExecutionPlan,
    PlannedSubIntent,
    SplitIntentResult,
    StructuredIntent,
    classify_clear_single_intent,
    detect_supported_compound_intent,
    split_intents,
    split_intents_with_metadata,
    merge_answers,
)
from scout.agents.tool_guard import (
    clear_inventory_argument_constraints,
    reset_guard,
    set_inventory_argument_constraints,
)
from scout.agents.claims import ClaimType, propose_claims
from scout.agents.result_normalization import (
    evidence_entries_from_tool_messages,
    extract_product_candidates,
    extract_tool_result_candidates,
    normalize_agent_result,
)
from scout.agents.verification import verify_claims, verify_price_grounding
from scout.agents.evidence import (
    clear_evidence_context,
    get_evidence_entries,
    get_tool_call_records,
    make_sub_intent_id,
    record_tool_call,
    start_evidence_context,
)
from scout.agents.execution import (
    _category_from_text,
    _continue_after_tool_evidence,
    _execute_read_only_tool,
    _external_offer_args,
    _inventory_constraints_from_structured_intent,
    _internal_products_satisfy_explicit_request,
    _product_contains_terms,
    _record_tool_first,
    _run_tool_first_if_possible,
    _scout_only_requested,
    _tool_first_call,
    _unique_product_candidate,
)
from scout.agents.execution.model_runtime import (
    ProviderWorkflowError,
    _invoke_agent_with_timing,
    _invoke_graph_with_timing,
    _is_provider_or_transport_error,
    _record_handled_provider_error,
    _wait_for,
)
from scout.agents.context import (
    CONTEXT_KEYS,
    _context_product,
    _normalize_context_text,
    _product_id_from_inventory_subject,
    _safe_conversation_context,
    _update_context_for_clarification,
    _update_context_from_verified_turn,
)
from scout.agents.context.followups import (
    BUDGET_PATTERN,
    CATEGORY_FOLLOW_UP_LABELS,
    FOLLOW_UP_PRODUCT_RE,
    ORDER_FOLLOW_UP_RE,
    USE_CASE_RE,
    _category_only_pending_follow_up,
    _contextual_split_result,
    _continue_pending_clarification,
    _extract_order_id_from_text,
    _join_context_labels,
    _parse_max_budget,
    _parse_requested_color,
    _parse_requested_size,
    _parse_requested_store_name,
    _pending_context_value,
    _record_context_resolution,
    _resolve_context_product,
    _resolve_follow_up_intent,
    _resolve_order_follow_up_intent,
)
from scout.agents.routing import (
    DETERMINISTIC_CONVERSATIONAL_REPLIES,
    DIRECT_ROUTE_SPECIALISTS,
    RECOVERY_ALTERNATIVE_RE,
    RECOVERY_BEST_FULFILLMENT_RE,
    RECOVERY_DELIVERY_RE,
    RECOVERY_STORE_RE,
    RECOVERY_TRAVEL_AVOIDANCE_RE,
    RECOVERY_URGENCY_RE,
    _deterministic_conversational_reply,
    _direct_route_agent,
    _get_specialist_registry,
    _is_scout_similar_products_request,
    _out_of_stock_recovery_action,
)
from scout.agents.verification_flow import (
    CORRECTABLE_REJECTION_CODES,
    CORRECTION_FINALIZATION_ALLOWANCE_SECONDS,
    CORRECTION_SPECIALISTS,
    EvidenceCompletionDecision,
    FinalizedResponse,
    _approved_claims_by_subject,
    _attempt_targeted_correction,
    _build_correction_instruction,
    _choose_better_response,
    _finalize_verified_response,
    _has_enough_time_for_correction,
    _has_essential_correctable_rejection,
    _initial_availability_result_is_sufficient,
    _initial_product_result_is_sufficient,
    _iter_completion_facts,
    _is_product_result,
    _normalized_claim_value,
    _parse_max_budget,
    _parse_requested_store,
    _product_identity_is_approved,
    _product_price_is_approved,
    _product_satisfies_budget,
    _requested_domain,
    _response_usefulness,
    _should_attempt_targeted_correction,
    _unique_text,
)
from scout.agents.comparison import (
    _comparison_ranking_criteria,
    _comparison_reply,
    _comparison_score,
    _finalize_product_comparison,
    _has_highest_rating,
    _safe_float,
)
from scout.agents.orchestration import (
    MultiIntentExecutionState,
    _dedupe_products_for_multi_intent,
    _empty_subgoal_response,
    _execute_multi_subgoal,
    _finalize_current_evidence,
    _has_pending_external_fallback,
    _inventory_context_message,
    _looks_like_contextual_comparison,
    _new_multi_intent_state,
    _record_multi_intent_supervisor_path,
    _record_multi_subgoal,
    _run_multi_intent_plan,
    _seed_comparison_products_from_context,
    _store_selected_products,
    _subgoal_limitation,
    plan_category,
)
from scout.agents.orchestration.turn_executor import (
    HANDOFF_MARKERS,
    MAX_HISTORY_MESSAGES,
    SPECIALIST_NAMES,
    _alternatives_no_match,
    _result_contains_messages,
    _run_direct_specialist_turn,
    _similar_products_no_match_reply,
    _trim_history_for_model,
    execute_single_intent_turn,
    extract_text,
    get_final_reply,
)
from scout.agents.orchestration.turn_context import TurnExecutionContext

_ORIGINAL_SPLIT_INTENTS = split_intents


def _set_order_tool_auth_context(context: dict | None):
    from scout.mcp_server import server as local_tools

    safe_context = _safe_conversation_context(context)
    customer_id = safe_context.get("authenticated_customer_id") if safe_context else None
    return local_tools.set_authenticated_customer_id(customer_id)


def _reset_order_tool_auth_context(token) -> None:
    from scout.mcp_server import server as local_tools

    local_tools.reset_authenticated_customer_id(token)


def _decimal_money(value) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None



def _split_intents_for_supervisor(model, message: str) -> SplitIntentResult:
    if split_intents is not _ORIGINAL_SPLIT_INTENTS:
        return SplitIntentResult(sub_intents=split_intents(model, message))
    return split_intents_with_metadata(model, message)


def _get_specialist_registry(app) -> dict:
    registry = getattr(app, "scout_specialists", None)
    return registry if isinstance(registry, dict) else {}


def _direct_route_agent(app, structured_intent: StructuredIntent | None, sub_intents: list[str]) -> str | None:
    if structured_intent is None or len(sub_intents) != 1:
        return None
    if structured_intent.needs_clarification:
        return None
    specialist_name = DIRECT_ROUTE_SPECIALISTS.get(structured_intent.request_type)
    if specialist_name is None:
        return None
    return specialist_name if specialist_name in _get_specialist_registry(app) else None


def _deterministic_conversational_reply(structured_intent: StructuredIntent | None, sub_intents: list[str]) -> str | None:
    if structured_intent is None or len(sub_intents) != 1:
        return None
    if structured_intent.request_type == "shopping_clarification" and structured_intent.clarification_question:
        return structured_intent.clarification_question
    return DETERMINISTIC_CONVERSATIONAL_REPLIES.get(structured_intent.request_type)


DIRECT_ROUTE_SPECIALISTS = {
    "product_recommendation": "recommend_agent",
    "similar_products": "recommend_agent",
    "inventory_availability": "inventory_agent",
    "store_availability": "inventory_agent",
    "order_status": "order_agent",
    "return_eligibility": "order_agent",
    "policy_question": "policy_agent",
    "external_offer": "external_offer_agent",
}
DETERMINISTIC_CONVERSATIONAL_REPLIES = {
    "greeting": "Hi — how can I help you shop today?",
    "thanks": "You're welcome — happy to help.",
    "purchase_execution": (
        "I can help you select the product and prepare your cart, but payment must be "
        "completed through Scout's secure storefront checkout."
    ),
    "shopping_clarification": (
        "What type of item are you looking for, and what budget would you like to stay within?"
    ),
    "out_of_scope": (
        "I don’t have live access for that kind of request here, but I can help with Scout products, "
        "availability, orders, checkout guidance, and return or refund policy."
    ),
}

def _get_final_reply_for_agent(messages: list, agent_name: str) -> str:
    for msg in reversed(messages):
        if getattr(msg, "name", None) != agent_name:
            continue
        text = extract_text(getattr(msg, "content", ""))
        stripped = text.strip()
        if stripped and stripped not in HANDOFF_MARKERS and not stripped.startswith("NEEDS_EXTERNAL_CHECK"):
            return text
    return "I couldn’t verify a corrected answer from the selected specialist."


def _extract_product_dicts(value, results: list[dict]) -> None:
    """Recursively flattens ANY nesting/encoding shape a tool result might
    arrive in — different providers structure MCP tool content differently
    (Claude sends a JSON-encoded STRING containing a nested list of
    {"type": "text", "text": "<json>"} blocks; Groq/Ollama/Gemini send an
    actual Python list of similar blocks; either can nest one or more
    levels deep). Rather than handle each shape as a separate branch, this
    walks the structure generically: strings get JSON-decoded and
    recursed into, lists get each item recursed into, and a dict is
    either a wrapper (has "text" -> decode and recurse into that) or an
    actual product (has "price"/"external_product_id" -> keep it)."""
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return
        _extract_product_dicts(parsed, results)
        return

    if isinstance(value, list):
        for item in value:
            _extract_product_dicts(item, results)
        return

    if isinstance(value, dict):
        if "price" in value or "external_product_id" in value:
            results.append(value)
            return
        if "text" in value:
            _extract_product_dicts(value["text"], results)
            return
        # unrecognized dict shape — skip silently rather than error


def _parse_json_blocks(content) -> list[dict]:
    """Entry point: flattens a tool message's content (any shape) into a
    plain list of product dicts."""
    results: list[dict] = []
    _extract_product_dicts(content, results)
    return results


def extract_products(messages: list) -> list[dict]:
    """Scans the message trace for actual tool results from product-bearing
    tools and returns them as structured data, so the frontend can render
    real product cards instead of parsing prose. Only pulls from genuine
    tool results — never invents or infers products."""
    return extract_tool_result_candidates(messages)


def _supports_turn_context(callable_obj) -> bool:
    try:
        return "turn_context" in inspect.signature(callable_obj).parameters
    except (TypeError, ValueError):
        return True


async def _call_run_single_intent(
    app,
    history: list[dict],
    sub_intent: str,
    debug: bool,
    conversation_context: dict | None,
    turn_context: TurnExecutionContext,
):
    if _supports_turn_context(_run_single_intent):
        if conversation_context is None:
            return await _run_single_intent(app, history, sub_intent, debug, turn_context=turn_context)
        return await _run_single_intent(app, history, sub_intent, debug, conversation_context, turn_context=turn_context)
    if conversation_context is None:
        return await _run_single_intent(app, history, sub_intent, debug)
    return await _run_single_intent(app, history, sub_intent, debug, conversation_context)


def _call_run_single_intent_streaming(
    app,
    history: list[dict],
    sub_intent: str,
    debug: bool,
    conversation_context: dict | None,
    turn_context: TurnExecutionContext,
):
    if _supports_turn_context(_run_single_intent_streaming):
        if conversation_context is None:
            return _run_single_intent_streaming(app, history, sub_intent, debug, turn_context=turn_context)
        return _run_single_intent_streaming(app, history, sub_intent, debug, conversation_context, turn_context=turn_context)
    if conversation_context is None:
        return _run_single_intent_streaming(app, history, sub_intent, debug)
    return _run_single_intent_streaming(app, history, sub_intent, debug, conversation_context)


async def _run_single_intent_body(
    app,
    history: list[dict],
    sub_intent: str,
    debug: bool,
    conversation_context: dict | None = None,
    *,
    turn_context: TurnExecutionContext | None = None,
):
    try:
        async for kind, payload in execute_single_intent_turn(
            app,
            history,
            sub_intent,
            debug,
            conversation_context,
            stream_progress=False,
            turn_context=turn_context,
        ):
            if kind == "result":
                return payload
        return "Something went wrong processing that — could you try again?", []
    except ProviderWorkflowError:
        history.append({"role": "assistant", "content": SAFE_TIMEOUT_REPLY})
        return SAFE_TIMEOUT_REPLY, []


async def _run_single_intent(
    app,
    history: list[dict],
    sub_intent: str,
    debug: bool,
    conversation_context: dict | None = None,
    *,
    turn_context: TurnExecutionContext | None = None,
):
    with timed_stage("complete_sub_intent"):
        try:
            return await _wait_for(
                _run_single_intent_body(app, history, sub_intent, debug, conversation_context, turn_context=turn_context),
                settings.SUB_INTENT_TIMEOUT_SECONDS,
                "sub-intent",
            )
        except TimeoutError:
            history.append({"role": "assistant", "content": SAFE_TIMEOUT_REPLY})
            return SAFE_TIMEOUT_REPLY, []
        except ProviderWorkflowError:
            history.append({"role": "assistant", "content": SAFE_TIMEOUT_REPLY})
            return SAFE_TIMEOUT_REPLY, []


async def _ask_body(app, history: list[dict], message: str, debug: bool = False, conversation_context: dict | None = None):
    """Splits the customer message into sub-intents (usually just one),
    runs each through the existing supervisor/specialist graph separately,
    then merges the answers into one natural reply. Products from ALL
    sub-intents are combined and returned. Returns
    (reply_text, updated_history, products)."""
    if not message or not message.strip():
        return "Please type a message.", history, []

    contextual_result = _contextual_split_result(message, conversation_context)
    splitter_model = get_chat_model()
    if contextual_result is not None:
        split_result = contextual_result
    else:
        with timed_stage("intent_splitting"):
            split_result = await _wait_for(
                asyncio.to_thread(_split_intents_for_supervisor, splitter_model, message),
                settings.MODEL_INVOCATION_TIMEOUT_SECONDS,
                "intent splitting",
            )
    sub_intents = split_result.sub_intents

    if split_result.execution_plan is not None and split_result.execution_plan.multi_intent:
        with timed_stage("multi_intent_supervisor_execution"):
            reply, products = await _run_multi_intent_plan(
                app,
                history,
                message,
                split_result.execution_plan,
                debug,
                conversation_context,
            )
        return reply, history, products

    if split_result.structured_intent is not None and split_result.structured_intent.request_type == "recommendation_selection":
        # Customer referred back to one of SEVERAL previously-shown
        # products by ordinal or name (e.g. "I like the second one").
        # This doesn't add to cart yet - it makes the SAME kind of
        # explicit offer Phase 2 makes automatically for a single
        # product, so the customer still has to say yes before anything
        # happens. Reuses the exact same pending_cart_offer + confirmation
        # mechanism already built and tested.
        selected_product_id = split_result.structured_intent.product_id
        selected_recommendation_id = split_result.structured_intent.recommendation_id
        product_name = None
        for item in (conversation_context or {}).get("active_selected_products") or []:
            if item.get("product_id") == selected_product_id:
                product_name = item.get("name")
                break

        if product_name:
            conversation_context["pending_cart_offer"] = {
                "product_id": selected_product_id,
                "product_name": product_name,
                "size": None,
                "color": None,
                "quantity": 1,
                "recommendation_id": selected_recommendation_id,
            }
            offer_reply = f"Want me to add the {product_name} to your cart?"
        else:
            offer_reply = "Sorry, I couldn't identify that item. Could you name it directly?"

        history.append({"role": "user", "content": sub_intents[0]})
        history.append({"role": "assistant", "content": offer_reply})
        return offer_reply, history, []

    if split_result.structured_intent is not None and split_result.structured_intent.request_type == "cart_add_confirmed":
        # Phase 2 - the customer just said "yes" to a real, specific
        # cart-add offer. This calls the EXACT SAME shared service
        # function api/cart.py's HTTP endpoint uses - re-validating real
        # stock/price fresh (never trusting the original recommendation's
        # snapshot), since availability can genuinely change between the
        # offer and the confirmation. No new AI tool exists here; this is
        # a deterministic response to a deterministically-recognized
        # confirmation, identical in effect to a real button click.
        session = SessionLocal()
        try:
            cart_result = add_to_cart_service(
                session,
                product_id=split_result.structured_intent.product_id,
                quantity=1,
                size=split_result.structured_intent.size,
                color=split_result.structured_intent.color,
                recommendation_id=split_result.structured_intent.recommendation_id,
            )
        finally:
            session.close()

        if cart_result.get("success"):
            confirm_reply = f"Added the {cart_result['name']} to your cart."
        else:
            confirm_reply = cart_result.get("error", "Sorry, I couldn't add that to your cart.")

        history.append({"role": "user", "content": sub_intents[0]})
        history.append({"role": "assistant", "content": confirm_reply})
        return confirm_reply, history, []

    direct_reply = _deterministic_conversational_reply(split_result.structured_intent, sub_intents)
    if direct_reply is not None:
        diagnostics = get_diagnostics()
        if diagnostics is not None and split_result.structured_intent is not None:
            if split_result.structured_intent.request_type == "purchase_execution":
                diagnostics.record("deterministic_purchase_boundary", 0, deterministic_purchase_boundary_used=True)
            if split_result.structured_intent.request_type == "shopping_clarification":
                diagnostics.record("deterministic_clarification", 0, deterministic_clarification_used=True)
        _update_context_for_clarification(conversation_context, split_result.structured_intent)
        history.append({"role": "user", "content": sub_intents[0]})
        history.append({"role": "assistant", "content": direct_reply})
        return direct_reply, history, []

    direct_specialist = _direct_route_agent(app, split_result.structured_intent, sub_intents)

    if debug and len(sub_intents) > 1:
        print(f"\n[Intent splitter] Detected {len(sub_intents)} sub-intents: {sub_intents}\n")

    all_products: list[dict] = []
    sub_answers: list[str] = []

    for sub_intent in sub_intents:
        turn_context = TurnExecutionContext(
            direct_specialist=direct_specialist,
            structured_intent=split_result.structured_intent if direct_specialist else None,
        )
        reply_text, products = await _call_run_single_intent(
            app, history, sub_intent, debug, conversation_context, turn_context
        )
        all_products.extend(products)
        sub_answers.append(reply_text)

    final_reply = merge_answers(splitter_model, message, sub_answers)

    return final_reply, history, all_products


async def ask(app, history: list[dict], message: str, debug: bool = False, conversation_context: dict | None = None):
    start_diagnostics()
    auth_token = _set_order_tool_auth_context(conversation_context)
    try:
        with timed_stage("complete_chat_request"):
            return await _wait_for(
                _ask_body(app, history, message, debug, conversation_context),
                settings.CHAT_REQUEST_TIMEOUT_SECONDS,
                "chat request",
            )
    except TimeoutError:
        history.append({"role": "assistant", "content": SAFE_TIMEOUT_REPLY})
        return SAFE_TIMEOUT_REPLY, history, []
    finally:
        _reset_order_tool_auth_context(auth_token)
        log_request_complete()
        clear_evidence_context()
        clear_diagnostics()


if __name__ == "__main__":
    async def _main():
        app, tool_manager = await build_supervisor_app()
        history: list[dict] = []
        # Persistent per-CLI-session conversation context, reusing the
        # SAME conversation-context system /chat already uses (see
        # _safe_conversation_context, CONTEXT_KEYS) — the CLI previously
        # never passed this at all, so every call defaulted to None and
        # follow-up product/size/color/store resolution never had
        # anything to work with. Confirmed via debug trace: this caused
        # a real bug where turn 3 of "Recommend a dress under $80" ->
        # "Is the black midi dress in a medium?" -> "Is it available at
        # Maple Grove?" called stores() with an invented,
        # non-canonical product_id instead of the real one already
        # resolved in turn 2.
        conversation_context: dict = {}
        print("Scout is ready. Type a message (Ctrl+C to quit).\n")
        while True:
            try:
                user_input = input("You: ")
            except (EOFError, KeyboardInterrupt):
                break
            if not user_input.strip():
                continue
            reply, history, products = await ask(
                app, history, user_input, debug=True, conversation_context=conversation_context
            )
            print(f"Scout: {reply}\n")
            if products:
                print(f"[products returned: {len(products)}]")
                for p in products:
                    print("  -", p.get("name"), p.get("price"), p.get("source", "internal"))
                print()
        await tool_manager.stop()

    asyncio.run(_main())


async def _run_single_intent_streaming_body(
    app,
    history: list[dict],
    sub_intent: str,
    debug: bool = False,
    conversation_context: dict | None = None,
    *,
    turn_context: TurnExecutionContext | None = None,
):
    """Streaming-aware sibling of _run_single_intent, used only by the
    /chat/stream endpoint. Yields ("progress", label) tuples as real
    LangGraph node transitions occur, then yields exactly one
    ("result", (reply_text, products)) tuple at the end — after running
    the SAME verification + one-correction-retry gate _run_single_intent
    uses. No partial/unverified text is ever yielded; only real progress
    signals and the final, checked answer.
    """
    async for item in execute_single_intent_turn(
            app,
            history,
            sub_intent,
            debug,
            conversation_context,
            stream_progress=True,
            turn_context=turn_context,
    ):
        yield item


async def _run_single_intent_streaming(
    app,
    history: list[dict],
    sub_intent: str,
    debug: bool = False,
    conversation_context: dict | None = None,
    *,
    turn_context: TurnExecutionContext | None = None,
):
    try:
        async with asyncio.timeout(settings.SUB_INTENT_TIMEOUT_SECONDS):
            with timed_stage("complete_sub_intent"):
                async for item in _run_single_intent_streaming_body(
                    app, history, sub_intent, debug, conversation_context, turn_context=turn_context
                ):
                    yield item
    except TimeoutError:
        mark_timeout()
        history.append({"role": "assistant", "content": SAFE_TIMEOUT_REPLY})
        yield ("result", (SAFE_TIMEOUT_REPLY, []))
    except ProviderWorkflowError:
        history.append({"role": "assistant", "content": SAFE_TIMEOUT_REPLY})
        yield ("result", (SAFE_TIMEOUT_REPLY, []))


async def ask_streaming(app, history: list[dict], message: str, debug: bool = False, conversation_context: dict | None = None):
    """Streaming-aware sibling of ask(). Yields ("progress", label) tuples
    as real steps occur across all sub-intents, then a final
    ("result", (final_reply, all_products)) tuple once everything has been
    processed and merged — same verification-gated pipeline as ask(),
    with progress visibility for the frontend.
    """
    start_diagnostics()
    auth_token = _set_order_tool_auth_context(conversation_context)
    try:
        async with asyncio.timeout(settings.CHAT_REQUEST_TIMEOUT_SECONDS):
            if not message or not message.strip():
                yield ("result", ("Please type a message.", []))
                return

            contextual_result = _contextual_split_result(message, conversation_context)
            splitter_model = get_chat_model()
            if contextual_result is not None:
                split_result = contextual_result
            else:
                with timed_stage("intent_splitting"):
                    split_result = await _wait_for(
                        asyncio.to_thread(_split_intents_for_supervisor, splitter_model, message),
                        settings.MODEL_INVOCATION_TIMEOUT_SECONDS,
                        "intent splitting",
                    )
            sub_intents = split_result.sub_intents

            if split_result.execution_plan is not None and split_result.execution_plan.multi_intent:
                yield ("progress", "understanding_request")
                with timed_stage("multi_intent_supervisor_execution"):
                    reply, products = await _run_multi_intent_plan(
                        app,
                        history,
                        message,
                        split_result.execution_plan,
                        debug,
                        conversation_context,
                    )
                yield ("result", (reply, products))
                return

            direct_reply = _deterministic_conversational_reply(split_result.structured_intent, sub_intents)
            if direct_reply is not None:
                diagnostics = get_diagnostics()
                if diagnostics is not None and split_result.structured_intent is not None:
                    if split_result.structured_intent.request_type == "purchase_execution":
                        diagnostics.record("deterministic_purchase_boundary", 0, deterministic_purchase_boundary_used=True)
                    if split_result.structured_intent.request_type == "shopping_clarification":
                        diagnostics.record("deterministic_clarification", 0, deterministic_clarification_used=True)
                _update_context_for_clarification(conversation_context, split_result.structured_intent)
                history.append({"role": "user", "content": sub_intents[0]})
                history.append({"role": "assistant", "content": direct_reply})
                yield ("result", (direct_reply, []))
                return

            direct_specialist = _direct_route_agent(app, split_result.structured_intent, sub_intents)

            all_products: list[dict] = []
            sub_answers: list[str] = []

            for sub_intent in sub_intents:
                turn_context = TurnExecutionContext(
                    direct_specialist=direct_specialist,
                    structured_intent=split_result.structured_intent if direct_specialist else None,
                )
                stream_items = _call_run_single_intent_streaming(
                    app, history, sub_intent, debug, conversation_context, turn_context
                )
                async for kind, payload in stream_items:
                    if kind == "progress":
                        yield ("progress", payload)
                    else:
                        reply_text, products = payload
                        all_products.extend(products)
                        sub_answers.append(reply_text)

            final_reply = merge_answers(splitter_model, message, sub_answers)
            yield ("result", (final_reply, all_products))
    except TimeoutError:
        mark_timeout()
        history.append({"role": "assistant", "content": SAFE_TIMEOUT_REPLY})
        yield ("result", (SAFE_TIMEOUT_REPLY, []))
    finally:
        _reset_order_tool_auth_context(auth_token)
        log_request_complete()
        clear_evidence_context()
        clear_diagnostics()
