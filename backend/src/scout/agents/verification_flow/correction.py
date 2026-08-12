from __future__ import annotations

import sys
import time
import re
from decimal import Decimal, InvalidOperation

from scout.config import settings
from scout.agents.claims import ClaimType
from scout.agents.context.followups import BUDGET_PATTERN, _parse_requested_size
from scout.agents.diagnostics import mark_correction_ran
from scout.agents.evidence import get_evidence_entries, start_evidence_context
from scout.agents.result_normalization import (
    evidence_entries_from_tool_messages,
    extract_product_candidates,
    normalize_agent_result,
)
from scout.agents.tool_guard import reset_guard
from scout.agents.verification import verify_price_grounding as _verify_price_grounding
from scout.agents.verification_flow.finalization import (
    FinalizedResponse,
    _choose_better_response,
    _finalize_verified_response,
)

CORRECTION_FINALIZATION_ALLOWANCE_SECONDS = 3.0
CORRECTABLE_REJECTION_CODES = {
    "missing_evidence",
    "unknown_evidence_id",
    "unsuccessful_evidence",
    "subject_mismatch",
    "field_not_present",
    "value_mismatch",
    "ambiguous_subject",
    "budget_exceeded",
    "wrong_agent_domain",
    "external_offer_not_labeled",
    "insufficient_policy_support",
}
CORRECTION_SPECIALISTS = {
    "recommend_agent",
    "inventory_agent",
    "order_agent",
    "external_offer_agent",
    "policy_agent",
}


def _supervisor_override(name: str, current=None):
    supervisor_module = sys.modules.get("scout.agents.supervisor")
    override = getattr(supervisor_module, name, None) if supervisor_module is not None else None
    if override is not None and override is not current:
        return override
    return current


def _get_specialist_registry(app) -> dict:
    registry = getattr(app, "scout_specialists", None)
    return registry if isinstance(registry, dict) else {}


def _decimal_money(value) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def _should_attempt_targeted_correction(
    finalized: FinalizedResponse,
    *,
    correction_attempted: bool,
    specialists: dict,
    customer_message: str = "",
    correction_deadline: float | None = None,
    now: float | None = None,
) -> bool:
    if correction_attempted or finalized.verification_result.verified:
        return False

    if _initial_product_result_is_sufficient(finalized, customer_message=customer_message):
        return False

    if not _has_enough_time_for_correction(correction_deadline, now=now):
        return False

    correction_agent = finalized.verification_result.correction_agent
    if correction_agent not in CORRECTION_SPECIALISTS or correction_agent not in specialists:
        return False

    rejected_claims = finalized.verification_result.rejected_claims
    if not rejected_claims:
        return False

    specialist_rejections = [
        rejected
        for rejected in rejected_claims
        if rejected.responsible_agent == correction_agent
    ]
    if not specialist_rejections:
        return False

    if not _has_essential_correctable_rejection(finalized, specialist_rejections):
        return False

    if any(
        rejected.responsible_agent not in {correction_agent, "supervisor", None}
        for rejected in rejected_claims
    ):
        return False

    return all(
        rejected.responsible_agent == correction_agent
        and rejected.reason_code in CORRECTABLE_REJECTION_CODES
        for rejected in specialist_rejections
    )


def _initial_product_result_is_sufficient(finalized: FinalizedResponse, *, customer_message: str) -> bool:
    requested_domain = _requested_domain(customer_message)
    if requested_domain in {"inventory", "store"}:
        return _initial_availability_result_is_sufficient(finalized, customer_message=customer_message, requested_domain=requested_domain)
    if requested_domain != "product":
        return False
    if not _is_product_result(finalized):
        return False
    if not finalized.products:
        return False
    approved_claims = _approved_claims_by_subject(finalized)
    max_budget = _parse_max_budget(customer_message)
    for product in finalized.products:
        if not isinstance(product, dict):
            continue
        subject_id = product.get("external_product_id") or product.get("product_id")
        if not subject_id:
            continue
        subject_claims = approved_claims.get(str(subject_id), {})
        if not _product_identity_is_approved(product, subject_claims):
            continue
        if not _product_price_is_approved(product, subject_claims):
            continue
        if not _product_satisfies_budget(product, max_budget):
            continue
        passed, _ = _supervisor_override("verify_price_grounding", _verify_price_grounding)(finalized.reply, finalized.products, customer_message=customer_message)
        if passed:
            return True
    return False


def _initial_availability_result_is_sufficient(
    finalized: FinalizedResponse,
    *,
    customer_message: str,
    requested_domain: str,
) -> bool:
    if not finalized.reply or not finalized.products and not finalized.proposed_claims:
        return False
    approved_claims = [
        claim
        for claim in finalized.proposed_claims
        if claim.claim_id in set(finalized.verification_result.approved_claim_ids)
    ]
    if requested_domain == "inventory":
        requested_size = _parse_requested_size(customer_message)
        for claim in approved_claims:
            if claim.claim_type not in {ClaimType.INVENTORY_AVAILABILITY.value, ClaimType.INVENTORY_QUANTITY.value}:
                continue
            if requested_size and f":size:{requested_size}" not in str(claim.subject_id):
                continue
            passed, _ = _supervisor_override("verify_price_grounding", _verify_price_grounding)(finalized.reply, finalized.products, customer_message=customer_message)
            return passed
    if requested_domain == "store":
        requested_store = _parse_requested_store(customer_message)
        store_claims = {
            claim.subject_id: claim.value
            for claim in approved_claims
            if claim.claim_type == ClaimType.STORE_IDENTITY.value and claim.field == "store_name"
        }
        matching_store_ids = {
            store_id
            for store_id, store_name in store_claims.items()
            if requested_store is None or str(store_name).strip().lower() == requested_store
        }
        for claim in approved_claims:
            if claim.claim_type not in {ClaimType.INVENTORY_AVAILABILITY.value, ClaimType.INVENTORY_QUANTITY.value, ClaimType.PICKUP_AVAILABILITY.value}:
                continue
            subject = str(claim.subject_id)
            if any(f":store:{store_id}" in subject for store_id in matching_store_ids):
                passed, _ = _supervisor_override("verify_price_grounding", _verify_price_grounding)(finalized.reply, finalized.products, customer_message=customer_message)
                return passed
    return False


def _requested_domain(customer_message: str) -> str:
    message = (customer_message or "").lower()
    if any(term in message for term in ("order", "tracking")):
        return "order"
    if any(term in message for term in ("policy", "refund", "return")):
        return "policy"
    if any(term in message for term in ("third-party", "third party", "external")):
        return "external"
    if any(term in message for term in ("store", " at ")):
        return "store"
    if any(term in message for term in ("available", "availability", "stock", "in stock", "size")) or _parse_requested_size(message):
        return "inventory"
    return "product"


def _parse_requested_store(customer_message: str) -> str | None:
    match = re.search(r"\b(?:at|in|near)\s+([A-Z][A-Za-z]*(?:\s+[A-Z][A-Za-z]*){0,3})\b", customer_message or "")
    return match.group(1).strip().lower() if match else None


def _is_product_result(finalized: FinalizedResponse) -> bool:
    if finalized.products:
        return True
    product_claim_types = {
        ClaimType.PRODUCT_IDENTITY.value,
        ClaimType.PRODUCT_PRICE.value,
        ClaimType.PRODUCT_RATING.value,
        ClaimType.PROMOTION.value,
        ClaimType.EXTERNAL_OFFER_IDENTITY.value,
        ClaimType.EXTERNAL_OFFER_PRICE.value,
        ClaimType.EXTERNAL_OFFER_VENDOR.value,
    }
    return any(getattr(claim, "claim_type", None) in product_claim_types for claim in finalized.proposed_claims)


def _approved_claims_by_subject(finalized: FinalizedResponse) -> dict[str, dict[str, set]]:
    approved_ids = set(finalized.verification_result.approved_claim_ids)
    by_subject: dict[str, dict[str, set]] = {}
    for claim in finalized.proposed_claims:
        if claim.claim_id not in approved_ids or claim.subject_id is None:
            continue
        subject_claims = by_subject.setdefault(str(claim.subject_id), {})
        subject_claims.setdefault(str(claim.claim_type), set()).add((claim.field, _normalized_claim_value(claim.value)))
    return by_subject


def _product_identity_is_approved(product: dict, subject_claims: dict[str, set]) -> bool:
    name = _normalized_claim_value(product.get("name"))
    identity_claims = subject_claims.get(ClaimType.PRODUCT_IDENTITY.value, set()) | subject_claims.get(
        ClaimType.EXTERNAL_OFFER_IDENTITY.value, set()
    )
    return any(field in {"name", "product_name"} and value == name for field, value in identity_claims)


def _product_price_is_approved(product: dict, subject_claims: dict[str, set]) -> bool:
    if product.get("price") is None:
        return False
    price = _normalized_claim_value(product.get("price"))
    price_claims = subject_claims.get(ClaimType.PRODUCT_PRICE.value, set()) | subject_claims.get(
        ClaimType.EXTERNAL_OFFER_PRICE.value, set()
    )
    return ("price", price) in price_claims


def _product_satisfies_budget(product: dict, max_budget: Decimal | None) -> bool:
    if max_budget is None:
        return True
    price = _decimal_money(product.get("price"))
    return price is not None and price <= max_budget


def _has_essential_correctable_rejection(finalized: FinalizedResponse, specialist_rejections: list) -> bool:
    claims_by_id = {claim.claim_id: claim for claim in finalized.proposed_claims}
    optional_product_claims = {ClaimType.PRODUCT_RATING.value, ClaimType.PROMOTION.value}
    for rejected in specialist_rejections:
        claim = claims_by_id.get(rejected.claim_id)
        if claim is None:
            return True
        if claim.claim_type not in optional_product_claims:
            return True
    return False


def _has_enough_time_for_correction(correction_deadline: float | None, *, now: float | None = None) -> bool:
    if correction_deadline is None:
        return True
    current = time.monotonic() if now is None else now
    required = settings.MODEL_INVOCATION_TIMEOUT_SECONDS + CORRECTION_FINALIZATION_ALLOWANCE_SECONDS
    return correction_deadline - current >= required


def _parse_max_budget(customer_message: str) -> Decimal | None:
    matches = BUDGET_PATTERN.findall(customer_message or "")
    if len(matches) != 1:
        return None
    return _decimal_money(matches[0])


def _normalized_claim_value(value) -> str:
    money = _decimal_money(value)
    if money is not None:
        return f"{money:.2f}"
    return str(value).strip().lower()


def _build_correction_instruction(
    *,
    original_request: str,
    finalized: FinalizedResponse,
) -> str:
    verification_result = finalized.verification_result
    claims_by_id = {claim.claim_id: claim for claim in finalized.proposed_claims}
    rejected_claims = verification_result.rejected_claims
    claim_types = _unique_text([
        claims_by_id[rejected.claim_id].claim_type
        for rejected in rejected_claims
        if rejected.claim_id in claims_by_id
    ])
    subject_ids = _unique_text([
        claims_by_id[rejected.claim_id].subject_id
        for rejected in rejected_claims
        if rejected.claim_id in claims_by_id
    ])
    reason_codes = _unique_text([rejected.reason_code for rejected in rejected_claims])
    missing = _unique_text([
        missing
        for rejected in rejected_claims
        for missing in rejected.missing_evidence
        if missing
    ])

    return (
        f"Recheck this customer request: {original_request}. "
        f"Affected claim types: {', '.join(claim_types) or 'unknown'}. "
        f"Affected subjects or evidence categories: {', '.join(subject_ids or missing) or 'unknown'}. "
        f"Safe rejection categories: {', '.join(reason_codes)}. "
        "Use only your approved tools. Return only facts directly supported by new tool results. "
        "Do not repeat unsupported facts."
    )


def _unique_text(values: list) -> list[str]:
    seen = set()
    output = []
    for value in values:
        if not value:
            continue
        text = str(value)
        if text in seen:
            continue
        seen.add(text)
        output.append(text)
    return output


async def _attempt_targeted_correction(
    app,
    *,
    initial: FinalizedResponse,
    customer_message: str,
    correction_attempted: bool,
    sub_intent_id: str,
    correction_deadline: float | None = None,
) -> FinalizedResponse:
    specialists = _get_specialist_registry(app)
    if not _should_attempt_targeted_correction(
        initial,
        correction_attempted=correction_attempted,
        specialists=specialists,
        customer_message=customer_message,
        correction_deadline=correction_deadline,
    ):
        return initial

    correction_agent = initial.verification_result.correction_agent
    mark_correction_ran()
    specialist = specialists[correction_agent]
    instruction = _build_correction_instruction(
        original_request=customer_message,
        finalized=initial,
    )

    wait_for = _supervisor_override("_wait_for")
    get_final_reply_for_agent = _supervisor_override("_get_final_reply_for_agent")
    if wait_for is None or get_final_reply_for_agent is None:
        return initial

    start_evidence_context(sub_intent_id, attempt_number=1)
    reset_guard(max_total_calls=10, max_identical_calls=1)
    try:
        result = await wait_for(
            specialist.ainvoke(
                {"messages": [{"role": "user", "content": instruction}]},
                config={"recursion_limit": 15},
            ),
            settings.SUB_INTENT_TIMEOUT_SECONDS,
            "targeted correction",
        )
    except (Exception, TimeoutError):
        return initial

    correction_normalized = normalize_agent_result(
        result=result,
        input_message_count=0,
        execution_mode="direct",
    )
    correction_messages = correction_normalized.messages
    corrected_reply = get_final_reply_for_agent(correction_messages, correction_agent)
    combined_evidence = get_evidence_entries()
    if not combined_evidence:
        combined_evidence = evidence_entries_from_tool_messages(
            normalized_messages=correction_messages,
            sub_intent_id=sub_intent_id,
            attempt_number=1,
        )
    corrected_products = extract_product_candidates(
        normalized_messages=correction_messages,
        evidence_entries=combined_evidence,
    )
    finalize = _supervisor_override("_finalize_verified_response", _finalize_verified_response)
    choose_better = _supervisor_override("_choose_better_response", _choose_better_response)
    corrected = finalize(
        original_reply=corrected_reply,
        products=corrected_products or initial.products,
        evidence_entries=combined_evidence,
        customer_message=customer_message,
    )
    return choose_better(initial, corrected)
