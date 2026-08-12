from __future__ import annotations

import re
from typing import Any

from scout.agents.claims import ClaimType

CONTEXT_KEYS = {
    "active_product_id",
    "active_product_name",
    "active_order_id",
    "authenticated_customer_id",
    "active_category",
    "active_selected_products",
    "requested_size",
    "requested_color",
    "requested_store",
    "requested_budget_max",
    "pending_intent",
    "pending_missing_fields",
}


def _safe_conversation_context(context: dict | None) -> dict | None:
    if context is None:
        return None
    for key in CONTEXT_KEYS:
        context.setdefault(key, [] if key in {"active_selected_products", "pending_missing_fields"} else None)
    return context


def _context_product(product: dict) -> dict | None:
    product_id = product.get("product_id")
    if not isinstance(product_id, str) or not product_id.strip():
        return None
    output = {"product_id": product_id}
    for key in ("name", "source", "category", "image_url"):
        if isinstance(product.get(key), str) and product[key].strip():
            output[key] = product[key]
    for key in ("price", "rating"):
        if product.get(key) is not None:
            output[key] = product[key]
    promotion = product.get("promotion")
    if isinstance(promotion, dict):
        output["promotion"] = promotion.copy()
    return output


def _product_id_from_inventory_subject(subject: str) -> str | None:
    match = re.search(r"(?:^|:)product:([^:]+)", subject)
    if match:
        return match.group(1)
    if subject.startswith("P"):
        return subject
    return None


def _normalize_context_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def _update_context_for_clarification(context: dict | None, structured_intent: Any | None) -> None:
    context = _safe_conversation_context(context)
    if context is None or structured_intent is None or structured_intent.request_type != "shopping_clarification":
        return
    if structured_intent.extraction_source == "deterministic_context_clarification":
        return
    context["pending_intent"] = "product_recommendation"
    if structured_intent.product_type:
        context["active_category"] = structured_intent.product_type
        context["pending_missing_fields"] = ["use_case", "budget"]
    else:
        context["pending_missing_fields"] = ["item_type", "budget"]


def _update_context_from_verified_turn(
    context: dict | None,
    *,
    finalized: Any,
    structured_intent: Any | None,
) -> None:
    context = _safe_conversation_context(context)
    if context is None:
        return
    approved_ids = set(finalized.verification_result.approved_claim_ids)
    approved_claims = [claim for claim in finalized.proposed_claims if claim.claim_id in approved_ids]
    products = [_context_product(product) for product in finalized.products if _context_product(product)]
    if products:
        context["active_selected_products"] = products
        if structured_intent and structured_intent.product_type:
            context["active_category"] = structured_intent.product_type
        if len(products) == 1:
            context["active_product_id"] = products[0]["product_id"]
            context["active_product_name"] = products[0].get("name")
        context["pending_intent"] = None
        context["pending_missing_fields"] = []

    product_names = {
        claim.subject_id: claim.value
        for claim in approved_claims
        if claim.claim_type == ClaimType.PRODUCT_IDENTITY.value and claim.field in {"name", "product_name"}
    }
    for claim in approved_claims:
        if claim.claim_type in {
            ClaimType.ORDER_STATUS.value,
            ClaimType.ORDER_TRACKING.value,
            ClaimType.PAYMENT_STATUS.value,
            ClaimType.RETURN_ELIGIBILITY.value,
        } and claim.subject_id:
            context["active_order_id"] = claim.subject_id
        subject = str(claim.subject_id or "")
        product_id = _product_id_from_inventory_subject(subject)
        if not product_id:
            continue
        context["active_product_id"] = product_id
        if product_names.get(product_id):
            context["active_product_name"] = product_names[product_id]
        if ":size:" in subject:
            context["requested_size"] = subject.split(":size:", 1)[1].split(":", 1)[0]
        if ":color:" in subject:
            context["requested_color"] = subject.split(":color:", 1)[1].split(":", 1)[0]
    if structured_intent:
        if structured_intent.budget_max is not None:
            context["requested_budget_max"] = structured_intent.budget_max
        if structured_intent.size:
            context["requested_size"] = structured_intent.size
        if structured_intent.color:
            context["requested_color"] = structured_intent.color
        if structured_intent.location:
            context["requested_store"] = structured_intent.location
