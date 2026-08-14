from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from scout.agents.context.conversation import _normalize_context_text, _safe_conversation_context
from scout.agents.diagnostics import get_diagnostics
from scout.agents.intent_splitter import (
    SplitIntentResult,
    StructuredIntent,
    classify_clear_single_intent,
    detect_supported_compound_intent,
)
from scout.agents.routing.recovery_router import (
    _is_scout_similar_products_request,
    _out_of_stock_recovery_action,
)

BUDGET_PATTERN = re.compile(
    r"\b(?:under|below|maximum|max|no more than|less than|up to)\s*\$?(\d+(?:\.\d{1,2})?)\b",
    re.IGNORECASE,
)
ORDER_FOLLOW_UP_RE = re.compile(
    r"\b(?:return|eligible|eligibility|refund|exchange|where|status|track|tracking|arrive|arrival|when|what did i order|what(?:'s| is) in (?:it|that order|this order)|items?)\b",
    re.IGNORECASE,
)
FOLLOW_UP_PRODUCT_RE = re.compile(r"\b(?:it|this|that one|the black one|what about|how about)\b", re.IGNORECASE)
USE_CASE_RE = re.compile(r"^[A-Za-z][A-Za-z\s-]{1,40}[?.!]?$")
CATEGORY_FOLLOW_UP_LABELS = {
    "dress": "dresses",
    "dresses": "dresses",
    "shoe": "shoes",
    "shoes": "shoes",
    "shirt": "shirts",
    "shirts": "shirts",
    "jacket": "jackets",
    "jackets": "jackets",
    "coat": "coats",
    "coats": "coats",
    "pants": "pants",
    "skirt": "skirts",
    "skirts": "skirts",
    "sweater": "sweaters",
    "sweaters": "sweaters",
    "top": "tops",
    "tops": "tops",
    "bag": "bags",
    "bags": "bags",
}


def _contextual_split_result(message: str, context: dict | None) -> SplitIntentResult | None:
    context = _safe_conversation_context(context)
    if context is None:
        return None

    compound_plan = detect_supported_compound_intent(message)
    if compound_plan is not None and compound_plan.multi_intent:
        return None

    cart_offer_pending = _continue_pending_cart_offer(message, context)
    if cart_offer_pending is not None:
        return cart_offer_pending

    pending = _continue_pending_clarification(message, context)
    if pending is not None:
        return pending

    resolved = _resolve_follow_up_intent(message, context)
    if resolved is None:
        return None
    if isinstance(resolved, str):
        return SplitIntentResult(
            sub_intents=[message],
            structured_intent=StructuredIntent(
                text=message,
                request_type="shopping_clarification",
                confidence=0.96,
                needs_clarification=True,
                clarification_question=resolved,
            ),
            fast_path_used=True,
            llm_splitter_invoked=False,
        )
    return SplitIntentResult(
        sub_intents=[resolved.text],
        structured_intent=resolved,
        fast_path_used=True,
        llm_splitter_invoked=False,
    )


def _continue_pending_cart_offer(message: str, context: dict) -> SplitIntentResult | None:
    """Recognizes a genuine 'yes' to a specific, real cart-add offer Scout
    just made (see api/chat.py, Phase 2). Deliberately strict - matches
    only a small, fixed set of whole-message affirmatives, not a
    substring anywhere in a longer message, since a loose match here
    could accidentally trigger a cart-add from an unrelated reply.
    """
    offer = context.get("pending_cart_offer")
    if not offer:
        return None

    normalized = (message or "").strip().strip(".!?").lower()
    AFFIRMATIVES = {"yes", "yeah", "yep", "sure", "please", "ok", "okay", "add it", "add to cart"}

    if normalized not in AFFIRMATIVES:
        context["pending_cart_offer"] = None
        return None

    context["pending_cart_offer"] = None
    structured = StructuredIntent(
        text=f"Add {offer.get('product_name')} to cart",
        request_type="cart_add_confirmed",
        confidence=0.98,
        product_id=offer.get("product_id"),
        recommendation_id=offer.get("recommendation_id"),
        size=offer.get("size"),
        color=offer.get("color"),
        extraction_source="deterministic_cart_offer_confirmation",
    )
    _record_context_resolution("cart_offer_confirmation", True, structured)
    return SplitIntentResult(
        sub_intents=[structured.text],
        structured_intent=structured,
        fast_path_used=True,
        llm_splitter_invoked=False,
    )


def _continue_pending_clarification(message: str, context: dict) -> SplitIntentResult | None:
    if context.get("pending_intent") != "product_recommendation":
        return None
    intent = classify_clear_single_intent(message)
    if intent is not None and intent.request_type not in {"shopping_clarification", "product_recommendation"}:
        return None
    if "budget" in (context.get("pending_missing_fields") or []):
        category_follow_up = _category_only_pending_follow_up(message)
        if category_follow_up and _parse_max_budget(message) is None:
            context["active_category"] = category_follow_up
            context["pending_missing_fields"] = ["budget"]
            question = f"Got it — {category_follow_up}. What budget should I use?"
            structured = StructuredIntent(
                text=message,
                request_type="shopping_clarification",
                confidence=0.96,
                product_type=category_follow_up,
                needs_clarification=True,
                clarification_question=question,
                extraction_source="deterministic_context_clarification",
            )
            _record_context_resolution("clarification_budget_followup", True, structured)
            return SplitIntentResult(
                sub_intents=[message],
                structured_intent=structured,
                fast_path_used=True,
                llm_splitter_invoked=False,
            )
    if not context.get("active_category"):
        return None
    normalized = _pending_context_value(message)
    if not normalized or not USE_CASE_RE.match(normalized):
        return None
    if any(term in normalized.lower() for term in ("order", "policy", "refund", "return", "checkout", "charge")):
        return None
    category = str(context["active_category"])
    merged = f"Find {normalized.lower()} {category}"
    merged_intent = StructuredIntent(
        text=merged,
        request_type="product_recommendation",
        confidence=0.93,
        product_type=category,
        extraction_source="deterministic_context_continuation",
    )
    _record_context_resolution("clarification_continuation", True, merged_intent)
    return SplitIntentResult(
        sub_intents=[merged],
        structured_intent=merged_intent,
        fast_path_used=True,
        llm_splitter_invoked=False,
    )


def _pending_context_value(message: str) -> str | None:
    text = (message or "").strip().strip(".!?")
    if not text:
        return None
    lowered = text.lower()
    budget = re.search(r"\b(?:under|below|max(?:imum)?|up to|less than)\s*\$?\d+(?:\.\d{1,2})?\b", lowered)
    if budget:
        return budget.group(0)
    for pattern in (
        r"\bfor\s+([a-z][a-z\s-]{1,30})(?:\s+specifically)?$",
        r"\bsomething\s+for\s+([a-z][a-z\s-]{1,30})(?:\s+specifically)?$",
        r"^(?:what about|how about)\s+([a-z][a-z\s-]{1,30})(?:\s+specifically)?$",
    ):
        match = re.search(pattern, lowered)
        if match:
            return match.group(1).strip()
    stripped = re.sub(r"^(?:what about|how about|something for)\s+", "", lowered).strip()
    stripped = re.sub(r"\s+specifically$", "", stripped).strip()
    return stripped or None


def _category_only_pending_follow_up(message: str) -> str | None:
    lowered = (message or "").strip().strip(".!?").lower()
    if not lowered:
        return None
    labels = []
    for term, label in sorted(CATEGORY_FOLLOW_UP_LABELS.items(), key=lambda item: len(item[0]), reverse=True):
        if re.search(rf"\b{re.escape(term)}\b", lowered) and label not in labels:
            labels.append(label)
    if not labels:
        return None
    remainder = lowered
    for term in CATEGORY_FOLLOW_UP_LABELS:
        remainder = re.sub(rf"\b{re.escape(term)}\b", " ", remainder)
    remainder = re.sub(
        r"\b(?:show|show me|find|recommend|looking for|i need|need|me|some|a|an|nice|good|great|pretty|cute|party|outfit|item|items|options?)\b",
        " ",
        remainder,
    )
    remainder = re.sub(r"\b(?:and|or|plus|also|both)\b|[,/&+]", " ", remainder)
    if re.sub(r"\s+", "", remainder):
        return None
    return _join_context_labels(labels)


def _join_context_labels(labels: list[str]) -> str:
    if len(labels) == 1:
        return labels[0]
    if len(labels) == 2:
        return f"{labels[0]} and {labels[1]}"
    return f"{', '.join(labels[:-1])}, and {labels[-1]}"


def _resolve_follow_up_intent(message: str, context: dict) -> StructuredIntent | str | None:
    base_intent = classify_clear_single_intent(message)
    normalized = (message or "").lower()
    order_follow_up = _resolve_order_follow_up_intent(message, context, base_intent)
    if order_follow_up is not None:
        return order_follow_up
    is_follow_up = bool(FOLLOW_UP_PRODUCT_RE.search(normalized))
    wants_similar_products = _is_scout_similar_products_request(normalized)
    recovery_action = _out_of_stock_recovery_action(normalized)
    if base_intent is None and not is_follow_up and not wants_similar_products and recovery_action is None:
        return None

    request_type = base_intent.request_type if base_intent else None
    if not wants_similar_products and recovery_action is None and request_type not in {None, "inventory_availability", "store_availability"}:
        return None
    if not wants_similar_products and recovery_action is None and request_type is None and not any(term in normalized for term in ("available", "availability", "stock", "pickup", "pick up", "pick it up", "online", "delivery", "shipping", "ship", "medium", "large", "small", "size")):
        return None

    product = _resolve_context_product(message, context)
    if product is None:
        if is_follow_up and context.get("active_selected_products"):
            return "Which product should I check?"
        return None
    if product == "ambiguous":
        return "Which product should I check?"

    size = (base_intent.size if base_intent else None) or _parse_requested_size(message) or context.get("requested_size")
    color = (base_intent.color if base_intent else None) or _parse_requested_color(message) or context.get("requested_color")
    location = (base_intent.location if base_intent else None) or _parse_requested_store_name(message)
    budget_max = (base_intent.budget_max if base_intent else None) or _parse_max_budget(message) or context.get("requested_budget_max")
    if recovery_action == "best_fulfillment":
        recovery_action = "store_availability" if location or context.get("requested_store") else "delivery"
    if recovery_action == "similar_products":
        wants_similar_products = True
    if wants_similar_products:
        product_name = product.get("name") or context.get("active_product_name") or product["product_id"]
        text = f"Find similar Scout products for product_id {product['product_id']} ({product_name})"
        if context.get("active_category") or (base_intent and base_intent.product_type):
            text += f" in {(base_intent.product_type if base_intent and base_intent.product_type else context.get('active_category'))}"
        if color:
            text += f" color {color}"
        if size:
            text += f" size {size}"
        if budget_max is not None:
            text += f" under ${float(budget_max):g}"
        text += "."
        structured = StructuredIntent(
            text=text,
            request_type="similar_products",
            confidence=0.96,
            product_type=(base_intent.product_type if base_intent and base_intent.product_type else context.get("active_category")),
            budget_max=float(budget_max) if budget_max is not None else None,
            size=size,
            color=color,
            product_id=product["product_id"],
            extraction_source="deterministic_conversation_context",
        )
        _record_context_resolution("similar_products_follow_up", True, structured)
        return structured
    wants_delivery_availability = recovery_action == "delivery" or any(term in normalized for term in ("online", "delivery", "shipping", "ship"))
    wants_store_availability = (
        recovery_action == "store_availability"
        or location
        or any(term in normalized for term in ("pickup", "pick up", "store", "nearby"))
    )
    resolved_type = "inventory_availability" if wants_delivery_availability else "store_availability" if wants_store_availability else "inventory_availability"
    if resolved_type == "store_availability" and not location and context.get("requested_store"):
        location = context.get("requested_store")
    if resolved_type == "store_availability" and not location:
        return "Which store or ZIP code should I use to check nearby availability?"
    product_name = product.get("name") or context.get("active_product_name") or product["product_id"]
    if wants_delivery_availability:
        text = f"Check online or delivery availability for product_id {product['product_id']} ({product_name})"
        if size:
            text += f" in size {size}"
        if color:
            text += f" color {color}"
        text += "."
    elif resolved_type == "store_availability":
        text = f"Check pickup availability for product_id {product['product_id']} ({product_name})"
        if location:
            text += f" at {location}"
        if size:
            text += f" in size {size}"
        if color:
            text += f" color {color}"
        text += "."
    else:
        text = f"Check stock for product_id {product['product_id']} ({product_name})"
        if size:
            text += f" in size {size}"
        if color:
            text += f" color {color}"
        text += "."
    structured = StructuredIntent(
        text=text,
        request_type=resolved_type,
        confidence=0.96,
        product_type=context.get("active_category") or (base_intent.product_type if base_intent else None),
        budget_max=float(budget_max) if budget_max is not None else None,
        size=size,
        color=color,
        location=location,
        product_id=product["product_id"],
        fulfillment_preference="delivery" if wants_delivery_availability else "pickup" if resolved_type == "store_availability" else None,
        extraction_source="deterministic_conversation_context",
    )
    _record_context_resolution("follow_up_resolution", True, structured)
    return structured


def _resolve_order_follow_up_intent(
    message: str,
    context: dict,
    base_intent: StructuredIntent | None,
) -> StructuredIntent | str | None:
    normalized = (message or "").lower()
    explicit_order_id = (base_intent.order_id if base_intent else None) or _extract_order_id_from_text(message)
    active_order_id = explicit_order_id or context.get("active_order_id")
    order_related = bool(ORDER_FOLLOW_UP_RE.search(normalized))
    if not order_related:
        return None
    if (
        base_intent
        and base_intent.request_type in {"policy_question", "purchase_execution"}
        and not explicit_order_id
        and not context.get("active_order_id")
    ):
        return None
    if not active_order_id:
        return "Which order ID should I check?"

    wants_return = any(term in normalized for term in ("return", "eligible", "eligibility", "refund", "exchange"))
    wants_status = any(
        term in normalized
        for term in (
            "where",
            "status",
            "track",
            "tracking",
            "arrive",
            "arrival",
            "when",
            "what did i order",
            "what's in it",
            "what is in it",
            "items",
        )
    )
    if not wants_return and not wants_status:
        return None

    request_type = "return_eligibility" if wants_return else "order_status"
    action = "Check return eligibility" if request_type == "return_eligibility" else "Check order status"
    structured = StructuredIntent(
        text=f"{action} for order {active_order_id}.",
        request_type=request_type,
        confidence=0.96,
        order_id=active_order_id,
        extraction_source="deterministic_order_context",
    )
    _record_context_resolution("order_follow_up_resolution", True, structured)
    return structured


def _resolve_context_product(message: str, context: dict) -> dict | str | None:
    selected = [item for item in context.get("active_selected_products") or [] if isinstance(item, dict) and item.get("product_id")]
    normalized_message = _normalize_context_text(message)
    explicit_matches = [
        item for item in selected
        if item.get("name") and _normalize_context_text(item["name"]) in normalized_message
    ]
    if len(explicit_matches) == 1:
        return explicit_matches[0]
    if len(explicit_matches) > 1:
        return "ambiguous"
    active_id = context.get("active_product_id")
    if active_id:
        for item in selected:
            if item.get("product_id") == active_id:
                return item
        return {"product_id": active_id, "name": context.get("active_product_name")}
    if len(selected) == 1 and FOLLOW_UP_PRODUCT_RE.search(message or ""):
        return selected[0]
    return None


def _record_context_resolution(stage: str, resolved: bool, intent: StructuredIntent | None = None) -> None:
    diagnostics = get_diagnostics()
    if diagnostics is not None:
        diagnostics.record(
            "conversation_context",
            0,
            context_stage=stage,
            context_resolved=resolved,
            selected_intent_type=intent.request_type if intent else None,
            llm_splitter_invoked=False if resolved else None,
        )


def _parse_requested_color(customer_message: str) -> str | None:
    lower = (customer_message or "").lower()
    for color in ("black", "white", "red", "blue", "green", "yellow", "pink", "purple", "brown", "gray", "grey", "navy", "cream", "beige"):
        if re.search(rf"\b{re.escape(color)}\b", lower):
            return color
    return None


def _parse_requested_store_name(customer_message: str) -> str | None:
    match = re.search(r"\b(?:at|in|near)\s+([A-Z][A-Za-z]*(?:\s+[A-Z][A-Za-z]*){0,3})\b", customer_message or "")
    return match.group(1).strip() if match else None


def _parse_requested_size(customer_message: str) -> str | None:
    match = re.search(
        r"\b(?:in\s+(?:a\s+)?|available\s+in\s+|size\s+)(extra small|extra large|xs|small|medium|large|xl|xxl|s|m|l)\b|"
        r"\b(extra small|extra large|xs|small|medium|large|xl|xxl|s|m|l)\s+size\b",
        customer_message or "",
        re.IGNORECASE,
    )
    if not match:
        return None
    value = next(group for group in match.groups() if group)
    return {
        "xs": "XS",
        "extra small": "XS",
        "s": "S",
        "small": "S",
        "m": "M",
        "medium": "M",
        "l": "L",
        "large": "L",
        "xl": "XL",
        "extra large": "XL",
        "xxl": "XXL",
    }.get(value.lower())


def _parse_max_budget(customer_message: str) -> Decimal | None:
    matches = BUDGET_PATTERN.findall(customer_message or "")
    if len(matches) != 1:
        return None
    return _decimal_money(matches[0])


def _decimal_money(value) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def _extract_order_id_from_text(text: str) -> str | None:
    match = re.search(r"\b(?:ORD[-\s]?\d{3,}|O\d{3,}|\d{3,})\b", text or "", re.IGNORECASE)
    return match.group(0).upper().replace(" ", "-") if match else None
