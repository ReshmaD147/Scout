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
    "pending_variant_choice",
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
    available_variants = []
    seen_variant_keys = set()

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
            context["requested_size"] = (
                subject.split(":size:", 1)[1].split(":", 1)[0]
            )

        if ":color:" in subject:
            context["requested_color"] = (
                subject.split(":color:", 1)[1].split(":", 1)[0]
            )

        if (
            claim.claim_type == ClaimType.INVENTORY_AVAILABILITY.value
            and claim.value is True
        ):
            size = (
                subject.split(":size:", 1)[1].split(":", 1)[0]
                if ":size:" in subject
                else None
            )
            color = (
                subject.split(":color:", 1)[1].split(":", 1)[0]
                if ":color:" in subject
                else None
            )

            if not (size or color):
                continue

            product_name = (
                product_names.get(product_id)
                or context.get("active_product_name")
            )
            if not product_name:
                continue

            # Recover the attribution belonging to this exact product.
            recommendation_id = None
            recommendation_session_id = None

            for selected_product in (
                context.get("active_selected_products") or []
            ):
                if selected_product.get("product_id") == product_id:
                    recommendation_id = selected_product.get(
                        "recommendation_id"
                    )
                    recommendation_session_id = selected_product.get(
                        "recommendation_session_id"
                    )
                    break

            variant = {
                "product_id": product_id,
                "product_name": product_name,
                "size": size,
                "color": color,
                "quantity": 1,
                "recommendation_id": recommendation_id,
                "recommendation_session_id": recommendation_session_id,
            }

            # Exact duplicate claims must never become duplicate choices.
            variant_key = (
                product_id,
                str(size or "").upper(),
                str(color or "").lower(),
            )

            if variant_key not in seen_variant_keys:
                seen_variant_keys.add(variant_key)
                available_variants.append(variant)

    # Verification may produce both an aggregate availability claim such as
    # "size L is available" and a more-specific claim such as
    # "black, size L is available". The aggregate claim is evidence about
    # the same real variant, not a separate customer choice.
    #
    # Preserve genuinely different variants, such as:
    #   white / 8
    #   black / 9
    #
    # but remove less-specific duplicates such as:
    #   None / L
    #   black / L
    def _is_dominated_variant(candidate: dict) -> bool:
        candidate_product = candidate.get("product_id")
        candidate_size = candidate.get("size")
        candidate_color = candidate.get("color")

        for other in available_variants:
            if other is candidate:
                continue
            if other.get("product_id") != candidate_product:
                continue

            other_size = other.get("size")
            other_color = other.get("color")

            # size-only aggregate is dominated by same size + real color
            if (
                candidate_size
                and not candidate_color
                and other_size == candidate_size
                and other_color
            ):
                return True

            # color-only aggregate is dominated by same color + real size
            if (
                candidate_color
                and not candidate_size
                and other_color == candidate_color
                and other_size
            ):
                return True

        return False

    available_variants = [
        variant
        for variant in available_variants
        if not _is_dominated_variant(variant)
    ]

    available_variant_count = len(available_variants)

    if available_variant_count == 1:
        available_cart_offer = available_variants[0]
    if structured_intent:
        if structured_intent.budget_max is not None:
            context["requested_budget_max"] = structured_intent.budget_max
        if structured_intent.size:
            context["requested_size"] = structured_intent.size
        if structured_intent.color:
            context["requested_color"] = structured_intent.color
        if structured_intent.location:
            context["requested_store"] = structured_intent.location
    if available_cart_offer and available_variant_count == 1:
        # Real bug fix, found via live testing before a demo: this loop
        # previously overwrote available_cart_offer for EVERY genuinely
        # in-stock variant claim, meaning when a size-check showed TWO
        # different, distinct in-stock variants (e.g. white/8 AND
        # black/9), the LAST one silently won, and a follow-up "add it
        # to my cart" added that variant without ever asking which one
        # the customer actually meant - a real, meaningful correctness
        # risk, not just a UX nicety. Now only sets an automatic
        # pending offer when there is genuinely exactly ONE available
        # variant to be unambiguous about.
        context["pending_cart_offer"] = available_cart_offer
        context["pending_variant_choice"] = None
    elif available_variant_count > 1:
        # Stop and ask the customer to choose, rather than guessing or
        # silently picking one - store the genuinely ambiguous options
        # so the next "add it to my cart" can trigger a specific,
        # helpful clarifying question naming them.
        context["pending_cart_offer"] = None
        context["pending_variant_choice"] = available_variants
