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
    "pending_store_availability_product_id",
    "pending_cart_offer",
    "completed_cart_add",
    "last_out_of_stock_product_id",
}


def _safe_conversation_context(context: dict | None) -> dict | None:
    if context is None:
        return None
    for key in CONTEXT_KEYS:
        context.setdefault(key, [] if key in {"active_selected_products", "pending_missing_fields"} else None)
    return context


def _context_product(product: dict) -> dict | None:
    product_id = product.get("product_id")
    external_product_id = product.get("external_product_id")
    if isinstance(product_id, str) and product_id.strip():
        output = {"product_id": product_id}
    elif isinstance(external_product_id, str) and external_product_id.strip():
        output = {"external_product_id": external_product_id, "source": "external"}
    else:
        return None
    for key in (
        "name",
        "source",
        "category",
        "brand",
        "image_url",
        "vendor_name",
        "click_url",
        "recommendation_id",
        "recommendation_session_id",
    ):
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
            if products[0].get("product_id"):
                context["active_product_id"] = products[0]["product_id"]
            context["active_product_name"] = products[0].get("name")
        context["pending_intent"] = None
        context["pending_missing_fields"] = []

    # Track a genuine, recent out-of-stock fact - this is the real gate
    # that _out_of_stock_recovery_action (recovery_router.py) should be
    # checked against, rather than pattern-matching keywords like
    # "today" or "store" against ANY message regardless of context.
    # Confirmed via a real bug: without this gate, "What is the weather
    # today?" was misclassified as a store-availability recovery
    # follow-up purely because it contained the word "today".
    out_of_stock_claims = [
        claim for claim in approved_claims
        if claim.claim_type == ClaimType.INVENTORY_AVAILABILITY.value and claim.value is False
    ]
    if out_of_stock_claims:
        context["last_out_of_stock_product_id"] = out_of_stock_claims[0].subject_id
    elif products or (structured_intent and structured_intent.request_type not in {None, "inventory_availability", "store_availability"}):
        # A genuinely new turn about something else clears this stale
        # flag, so it can't linger and incorrectly gate future messages
        # indefinitely.
        context["last_out_of_stock_product_id"] = None

    product_names = {
        claim.subject_id: claim.value
        for claim in approved_claims
        if claim.claim_type == ClaimType.PRODUCT_IDENTITY.value and claim.field in {"name", "product_name"}
    }
    available_cart_offer = None
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
        if claim.claim_type == ClaimType.INVENTORY_AVAILABILITY.value and claim.value is True:
            size = subject.split(":size:", 1)[1].split(":", 1)[0] if ":size:" in subject else None
            color = subject.split(":color:", 1)[1].split(":", 1)[0] if ":color:" in subject else None
            if size or color:
                product_name = product_names.get(product_id) or context.get("active_product_name")
                if product_name:
                    available_cart_offer = {
                        "product_id": product_id,
                        "product_name": product_name,
                        "size": size,
                        "color": color,
                        "quantity": 1,
                        "recommendation_id": None,
                        "recommendation_session_id": None,
                    }
    if structured_intent:
        if structured_intent.budget_max is not None:
            context["requested_budget_max"] = structured_intent.budget_max
        if structured_intent.size:
            context["requested_size"] = structured_intent.size
        if structured_intent.color:
            context["requested_color"] = structured_intent.color
        if structured_intent.location:
            context["requested_store"] = structured_intent.location
    if available_cart_offer:
        context["pending_cart_offer"] = available_cart_offer
