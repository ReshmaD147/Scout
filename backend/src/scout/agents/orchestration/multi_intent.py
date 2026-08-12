from __future__ import annotations

import re
import sys
from dataclasses import dataclass

from scout.agents.comparison import _finalize_product_comparison
from scout.agents.context import _safe_conversation_context, _update_context_from_verified_turn
from scout.agents.context.followups import _extract_order_id_from_text
from scout.agents.diagnostics import SAFE_TIMEOUT_REPLY, get_diagnostics, record_selected_specialist
from scout.agents.evidence import (
    clear_evidence_context,
    get_evidence_entries,
    make_sub_intent_id,
    start_evidence_context,
)
from scout.agents.execution.model_runtime import ProviderWorkflowError
from scout.agents.execution import (
    _execute_read_only_tool,
    _external_offer_args,
    _internal_products_satisfy_explicit_request,
)
from scout.agents.intent_splitter import ExecutionPlan, PlannedSubIntent, StructuredIntent, merge_answers
from scout.agents.model_provider import get_chat_model
from scout.agents.result_normalization import extract_product_candidates
from scout.agents.tool_guard import clear_inventory_argument_constraints, reset_guard
from scout.agents.verification_flow.finalization import FinalizedResponse, _finalize_verified_response


def _supervisor_override(name: str, current):
    supervisor_module = sys.modules.get("scout.agents.supervisor")
    override = getattr(supervisor_module, name, None) if supervisor_module is not None else None
    if override is not None and override is not current:
        return override
    return None


@dataclass
class MultiIntentExecutionState:
    multi_intent: bool
    execution_plan: ExecutionPlan
    selected_product_ids: list[str]
    selected_products: list[dict]
    comparison_candidates: list[dict]
    ranking_criteria: list[str]
    selected_best_product_id: str | None
    requested_stores: list[str]
    budget_max: float | None
    requested_size: str | None
    requested_color: str | None
    requested_store: str | None
    completed_subgoals: list[str]
    pending_subgoals: list[str]
    evidence_by_specialist: dict[str, list]
    current_subgoal: str | None = None
    next_subgoal: str | None = None


def _new_multi_intent_state(plan: ExecutionPlan) -> MultiIntentExecutionState:
    return MultiIntentExecutionState(
        multi_intent=True,
        execution_plan=plan,
        selected_product_ids=[],
        selected_products=[],
        comparison_candidates=[],
        ranking_criteria=[],
        selected_best_product_id=None,
        requested_stores=[plan.requested_store] if plan.requested_store else [],
        budget_max=plan.budget_max,
        requested_size=plan.requested_size,
        requested_color=plan.requested_color,
        requested_store=plan.requested_store,
        completed_subgoals=[],
        pending_subgoals=[subgoal.intent for subgoal in plan.subgoals],
        evidence_by_specialist={},
    )


def _record_multi_intent_supervisor_path(plan: ExecutionPlan) -> None:
    override = _supervisor_override("_record_multi_intent_supervisor_path", _record_multi_intent_supervisor_path)
    if override is not None:
        return override(plan)
    diagnostics = get_diagnostics()
    if diagnostics is not None:
        diagnostics.record(
            "supervisor_graph_invocation",
            0,
            supervisor_graph_entered=True,
            supervisor_model_called=False,
            deterministic_plan_used=True,
            planned_subgoals=[subgoal.intent for subgoal in plan.subgoals],
        )


def _record_multi_subgoal(state: MultiIntentExecutionState, subgoal: PlannedSubIntent, completed: bool) -> None:
    if completed and subgoal.intent not in state.completed_subgoals:
        state.completed_subgoals.append(subgoal.intent)
    state.pending_subgoals = [intent for intent in state.pending_subgoals if intent != subgoal.intent] if completed else state.pending_subgoals
    diagnostics = get_diagnostics()
    if diagnostics is not None:
        diagnostics.record(
            "multi_intent_subgoal",
            0,
            current_subgoal=subgoal.intent,
            next_subgoal=state.next_subgoal,
            completed=completed,
            completed_subgoals=list(state.completed_subgoals),
            pending_subgoals=list(state.pending_subgoals),
            selected_product_ids=list(state.selected_product_ids),
        )


async def _run_multi_intent_plan(
    app,
    history: list[dict],
    message: str,
    plan: ExecutionPlan,
    debug: bool = False,
    conversation_context: dict | None = None,
    provider_error_type: type[BaseException] | tuple[type[BaseException], ...] = ProviderWorkflowError,
) -> tuple[str, list[dict]]:
    _record_multi_intent_supervisor_path(plan)
    state = _new_multi_intent_state(plan)
    reused_context_products = _seed_comparison_products_from_context(state, message, conversation_context)
    history.append({"role": "user", "content": message})
    finalized_results: list[FinalizedResponse] = []
    all_products: list[dict] = []

    for index, subgoal in enumerate(plan.subgoals):
        state.current_subgoal = subgoal.intent
        state.next_subgoal = plan.subgoals[index + 1].intent if index + 1 < len(plan.subgoals) else None
        if any(dependency not in state.completed_subgoals for dependency in subgoal.depends_on):
            finalized_results.append(
                FinalizedResponse(
                    reply=_subgoal_limitation(subgoal),
                    products=[],
                    proposed_claims=[],
                    verification_result=type("Verification", (), {"approved_claim_ids": []})(),
                )
            )
            _record_multi_subgoal(state, subgoal, completed=False)
            continue
        if reused_context_products and subgoal.intent == "product_recommendation":
            _record_multi_subgoal(state, subgoal, completed=True)
            continue
        if subgoal.conditional == "internal_insufficient" and _internal_products_satisfy_explicit_request(
            state.selected_products,
            StructuredIntent(
                text=message,
                request_type="product_recommendation",
                confidence=0.9,
                product_type=plan.product_type,
                budget_max=plan.budget_max,
                color=plan.requested_color,
            ),
        ):
            _record_multi_subgoal(state, subgoal, completed=False)
            continue

        record_selected_specialist(subgoal.specialist)
        reset_guard(max_total_calls=10, max_identical_calls=1)
        sub_intent_id = make_sub_intent_id()
        start_evidence_context(sub_intent_id, attempt_number=0)
        try:
            finalized = await _execute_multi_subgoal(state, subgoal, message)
            evidence_entries = get_evidence_entries()
            state.evidence_by_specialist.setdefault(subgoal.specialist, []).extend(evidence_entries)
            completed = finalized.reply != SAFE_TIMEOUT_REPLY and (
                bool(finalized.verification_result.approved_claim_ids) or bool(finalized.products)
            )
            if subgoal.intent == "policy_question" and "third-party retailer return-policy evidence" in finalized.reply:
                completed = True
            if subgoal.intent == "product_recommendation":
                _store_selected_products(state, finalized.products)
                if _has_pending_external_fallback(plan) and not _internal_products_satisfy_explicit_request(
                    state.selected_products,
                    StructuredIntent(
                        text=message,
                        request_type="product_recommendation",
                        confidence=0.9,
                        product_type=plan.product_type,
                        budget_max=plan.budget_max,
                        color=plan.requested_color,
                    ),
                ):
                    _record_multi_subgoal(state, subgoal, completed=True)
                    continue
            if completed:
                if subgoal.intent == "product_recommendation":
                    _update_context_from_verified_turn(
                        conversation_context,
                        finalized=finalized,
                        structured_intent=StructuredIntent(
                            text=subgoal.text,
                            request_type="product_recommendation",
                            confidence=0.9,
                            product_type=plan.product_type,
                            budget_max=plan.budget_max,
                            color=plan.requested_color,
                        ),
                    )
                finalized_results.append(finalized)
                all_products.extend(finalized.products)
            else:
                finalized_results.append(
                    FinalizedResponse(
                        reply=_subgoal_limitation(subgoal),
                        products=[],
                        proposed_claims=[],
                        verification_result=finalized.verification_result,
                    )
                )
            _record_multi_subgoal(state, subgoal, completed=completed)
        except provider_error_type:
            finalized_results.append(
                FinalizedResponse(
                    reply=_subgoal_limitation(subgoal),
                    products=[],
                    proposed_claims=[],
                    verification_result=type("Verification", (), {"approved_claim_ids": []})(),
                )
            )
            _record_multi_subgoal(state, subgoal, completed=False)
        finally:
            clear_inventory_argument_constraints()
            clear_evidence_context()

    model_factory = _supervisor_override("get_chat_model", get_chat_model) or get_chat_model
    reply = merge_answers(model_factory(), message, [result.reply for result in finalized_results if result.reply])
    if not reply:
        reply = SAFE_TIMEOUT_REPLY
    history.append({"role": "assistant", "content": reply})
    return reply, _dedupe_products_for_multi_intent(all_products)


async def _execute_multi_subgoal(
    state: MultiIntentExecutionState,
    subgoal: PlannedSubIntent,
    original_message: str,
) -> FinalizedResponse:
    if subgoal.intent == "product_recommendation":
        args = {"query": subgoal.text, "top_n": 3}
        if state.budget_max is not None:
            args["max_price"] = state.budget_max
        await _execute_read_only_tool("recommend_products", args, agent_name="recommend_agent")
        return _finalize_current_evidence(subgoal, original_message)

    if subgoal.intent == "product_comparison":
        return _finalize_product_comparison(state, subgoal)

    if subgoal.intent in {"inventory_availability", "store_availability"}:
        if not state.selected_product_ids:
            return _empty_subgoal_response(subgoal, original_message)
        for product_id in state.selected_product_ids:
            if state.requested_size:
                args = {"product_id": product_id, "size": state.requested_size}
                if state.requested_color:
                    args["color"] = state.requested_color
                await _execute_read_only_tool("stock", args, agent_name="inventory_agent")
            if state.requested_store or subgoal.intent == "store_availability":
                args = {"product_id": product_id}
                if state.requested_store:
                    args["store_name"] = state.requested_store
                await _execute_read_only_tool("stores", args, agent_name="inventory_agent")
        return _finalize_current_evidence(
            subgoal,
            _inventory_context_message(subgoal.text or original_message, state.selected_products),
            extra_evidence=state.evidence_by_specialist.get("recommend_agent", []),
            products=state.selected_products,
        )

    if subgoal.intent == "policy_question":
        if "third-party" in subgoal.text.lower():
            return FinalizedResponse(
                reply="I do not have verified third-party retailer return-policy evidence for those outside offers.",
                products=[],
                proposed_claims=[],
                verification_result=type("Verification", (), {"approved_claim_ids": []})(),
            )
        for query in _policy_queries_for_subgoal(subgoal, original_message):
            await _execute_read_only_tool("retrieve_policy_chunks", {"query": query, "k": 3}, agent_name="policy_agent")
        return _finalize_current_evidence(subgoal, _policy_customer_message_for_subgoal(subgoal, original_message))

    if subgoal.intent == "external_offer":
        args = _external_offer_args(
            state.execution_plan.subgoals[0].text,
            category=plan_category(state),
            budget_max=state.budget_max,
            color=state.requested_color,
        )
        await _execute_read_only_tool("search_external_offers", args, agent_name="external_offer_agent")
        return _finalize_current_evidence(subgoal, original_message)

    if subgoal.intent == "order_status":
        order_id = _extract_order_id_from_text(subgoal.text)
        if order_id:
            await _execute_read_only_tool("orders", {"order_id": order_id}, agent_name="order_agent")
        return _finalize_current_evidence(subgoal, original_message)

    if subgoal.intent == "return_eligibility":
        order_id = _extract_order_id_from_text(subgoal.text)
        if order_id:
            await _execute_read_only_tool("return_eligibility", {"order_id": order_id}, agent_name="order_agent")
        return _finalize_current_evidence(subgoal, original_message)

    return _empty_subgoal_response(subgoal, original_message)


def _policy_queries_for_subgoal(subgoal: PlannedSubIntent, original_message: str) -> list[str]:
    queries = [subgoal.text]
    if "return eligibility" not in (subgoal.text or "").lower():
        return queries
    specific_query = _specific_return_policy_query(original_message)
    if specific_query and specific_query not in queries:
        queries.append(specific_query)
    return queries


def _policy_customer_message_for_subgoal(subgoal: PlannedSubIntent, original_message: str) -> str:
    if "return eligibility" not in (subgoal.text or "").lower():
        return original_message
    specific_query = _specific_return_policy_query(original_message)
    if specific_query:
        return "Explain Scout return policy for opened, used, worn, defective, or missing-tag items."
    return "Explain the Scout return policy rule for order return eligibility."


def _specific_return_policy_query(message: str) -> str | None:
    normalized = (message or "").lower()
    if any(term in normalized for term in ("opened", "open box", "unboxed", "used", "worn", "wear")):
        return "Scout return policy opened used worn items defective original tags"
    if any(term in normalized for term in ("damaged", "defective", "broken")):
        return "Scout return policy damaged defective items"
    if any(term in normalized for term in ("missing packaging", "without packaging", "missing tags", "without tags", "no tags")):
        return "Scout return policy original tags packaging"
    return None


def _finalize_current_evidence(
    subgoal: PlannedSubIntent,
    original_message: str,
    *,
    extra_evidence: list | None = None,
    products: list | None = None,
) -> FinalizedResponse:
    evidence_entries = [*(extra_evidence or []), *get_evidence_entries()]
    has_product_override = products is not None
    if subgoal.intent == "external_offer" and _external_no_match(evidence_entries):
        return FinalizedResponse(
            reply="Scout did not find an internal or verified third-party product matching all requested external-offer constraints.",
            products=[],
            proposed_claims=[],
            verification_result=type("Verification", (), {"approved_claim_ids": []})(),
        )
    products = products if products is not None else extract_product_candidates(normalized_messages=[], evidence_entries=evidence_entries)
    if subgoal.intent == "external_offer":
        products = [product for product in products if product.get("source") == "external"]
    elif subgoal.intent not in {"product_recommendation", "inventory_availability", "store_availability"}:
        products = []
    finalize = _supervisor_override("_finalize_verified_response", _finalize_verified_response) or _finalize_verified_response
    return finalize(
        original_reply="",
        products=products,
        evidence_entries=evidence_entries,
        customer_message=original_message if has_product_override else (subgoal.text or original_message),
    )


def _external_no_match(evidence_entries: list) -> bool:
    for entry in evidence_entries:
        if getattr(entry, "tool_name", None) != "search_external_offers" or not getattr(entry, "success", False):
            continue
        facts = getattr(entry, "normalized_facts", {})
        if isinstance(facts, dict) and facts.get("match_count") == 0 and facts.get("message"):
            return True
    return False


def _empty_subgoal_response(subgoal: PlannedSubIntent, original_message: str) -> FinalizedResponse:
    finalize = _supervisor_override("_finalize_verified_response", _finalize_verified_response) or _finalize_verified_response
    return finalize(
        original_reply="",
        products=[],
        evidence_entries=get_evidence_entries(),
        customer_message=subgoal.text or original_message,
    )


def _store_selected_products(state: MultiIntentExecutionState, products: list[dict]) -> None:
    state.selected_products = [product for product in products if isinstance(product, dict) and product.get("product_id")]
    state.selected_product_ids = [product["product_id"] for product in state.selected_products]


def _inventory_context_message(message: str, products: list[dict]) -> str:
    hints = [
        f"product_id {product['product_id']} ({product['name']})"
        for product in products
        if isinstance(product, dict)
        and isinstance(product.get("product_id"), str)
        and isinstance(product.get("name"), str)
    ]
    if not hints:
        return message
    return f"Check pickup availability for {', '.join(hints)}. {message}"


def _seed_comparison_products_from_context(
    state: MultiIntentExecutionState,
    message: str,
    conversation_context: dict | None,
) -> bool:
    if not _looks_like_contextual_comparison(message):
        return False
    if not any(subgoal.intent == "product_comparison" for subgoal in state.execution_plan.subgoals):
        return False
    context = _safe_conversation_context(conversation_context)
    if context is None:
        return False
    products = [
        product
        for product in context.get("active_selected_products") or []
        if isinstance(product, dict) and product.get("product_id")
    ]
    if len(products) < 2:
        return False
    _store_selected_products(state, products)
    return True


def _looks_like_contextual_comparison(message: str) -> bool:
    normalized = (message or "").lower()
    context_terms = {"compare these", "compare them", "compare those", "these three", "these 3", "pick the best"}
    comparison_terms = {"compare", "pick", "best", "rank"}
    return bool(
        any(term in normalized for term in context_terms)
        and any(term in normalized for term in comparison_terms)
    )


def _has_pending_external_fallback(plan: ExecutionPlan) -> bool:
    return any(subgoal.intent == "external_offer" and subgoal.conditional == "internal_insufficient" for subgoal in plan.subgoals)


def _subgoal_limitation(subgoal: PlannedSubIntent) -> str:
    labels = {
        "product_recommendation": "I couldn’t verify product recommendations for that part of the request.",
        "inventory_availability": "I couldn’t verify the requested inventory availability.",
        "store_availability": "I couldn’t verify the requested store availability.",
        "product_comparison": "I couldn’t compare verified products for that part of the request.",
        "policy_question": "I couldn’t verify the requested policy detail.",
        "order_status": "I couldn’t verify the requested order status.",
        "return_eligibility": "I couldn’t verify return eligibility for that order.",
        "external_offer": "I couldn’t verify a third-party offer for that part of the request.",
    }
    return labels.get(subgoal.intent, "I couldn’t verify that part of the request.")


def _dedupe_products_for_multi_intent(products: list[dict]) -> list[dict]:
    by_id = {}
    for product in products:
        key = product.get("product_id") or product.get("external_product_id")
        if key and key not in by_id:
            by_id[key] = product
    return list(by_id.values())


def plan_category(state: MultiIntentExecutionState) -> str:
    category = state.execution_plan.product_type or "products"
    if category == "dress":
        return "dresses"
    if category == "shoe":
        return "shoes"
    return category
