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

    rating_comparison = _resolve_rating_comparison_follow_up(message, context)
    if rating_comparison is not None:
        return SplitIntentResult(
            sub_intents=[rating_comparison.text],
            structured_intent=rating_comparison,
            fast_path_used=True,
            llm_splitter_invoked=False,
        )

    recommendation_action = _resolve_recommendation_action_follow_up(message, context)
    if recommendation_action is not None:
        return SplitIntentResult(
            sub_intents=[recommendation_action.text],
            structured_intent=recommendation_action,
            fast_path_used=True,
            llm_splitter_invoked=False,
        )

    compound_plan = detect_supported_compound_intent(message)
    if compound_plan is not None and compound_plan.multi_intent:
        return None

    pending_store_follow_up = _resolve_pending_store_location_follow_up(message, context)
    if pending_store_follow_up is not None:
        return SplitIntentResult(
            sub_intents=[pending_store_follow_up.text],
            structured_intent=pending_store_follow_up,
            fast_path_used=True,
            llm_splitter_invoked=False,
        )

    cart_offer_pending = _continue_pending_cart_offer(message, context)
    if cart_offer_pending is not None:
        return cart_offer_pending

    inventory_product_switch = _resolve_inventory_product_switch_follow_up(message, context)
    if inventory_product_switch is not None:
        return SplitIntentResult(
            sub_intents=[inventory_product_switch.text],
            structured_intent=inventory_product_switch,
            fast_path_used=True,
            llm_splitter_invoked=False,
        )

    recommendation_selection = _resolve_recommendation_selection(message, context)
    if recommendation_selection is not None:
        return recommendation_selection

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
    cart_add_confirmation = re.fullmatch(
        r"(?:can you |could you |please )?add (?:it|this|that)(?: (?:to|in) (?:my )?cart)?",
        normalized,
    )

    if normalized not in AFFIRMATIVES and not cart_add_confirmation:
        # A genuine clarifying question about the SAME pending item
        # (e.g. "is it available in medium first?") should PRESERVE the
        # offer, not discard it - the customer hasn't declined, they're
        # gathering more information before deciding. Only a message
        # that looks like a real topic change clears the offer.
        # Confirmed via a real, live-tested conversation: clearing here
        # unconditionally broke a genuinely common pattern (interrupting
        # a cart offer with a relevant question, then confirming).
        clarification_terms = (
            "available", "availability", "stock", "size", "medium",
            "large", "small", "color", "colour", "store", "nearby",
            "pickup", "pick up", "online", "delivery", "shipping", "ship",
        )
        if any(term in normalized for term in clarification_terms):
            return None
        context["pending_cart_offer"] = None
        return None

    context["pending_cart_offer"] = None
    structured = StructuredIntent(
        text=f"Add {offer.get('product_name')} to cart",
        request_type="cart_add_confirmed",
        confidence=0.98,
        product_id=offer.get("product_id"),
        recommendation_id=offer.get("recommendation_id"),
        recommendation_session_id=offer.get("recommendation_session_id"),
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


def _resolve_recommendation_selection(message: str, context: dict) -> SplitIntentResult | None:
    """Deterministic follow-up resolver for a customer selecting one of
    SEVERAL previously-shown products by ordinal or name - e.g. 'I like
    the second one', 'the Wrap Dress looks nice'. Deliberately a
    SEPARATE function from _resolve_follow_up_intent (the existing
    availability/order resolver) rather than an extension of it, since
    that function is narrowly, deliberately scoped to availability-style
    follow-ups already, and broadening it risked destabilizing working,
    tested logic. Triggers Phase 2's existing cart-offer mechanism once
    a specific product is identified - reuses register_recommendation's
    downstream path exactly, no new cart-add logic here at all.
    """
    selected = [item for item in context.get("active_selected_products") or [] if isinstance(item, dict) and item.get("product_id")]
    if len(selected) < 2:
        # Only meaningful when multiple products were genuinely shown -
        # a single-product case is already handled by Phase 2's
        # automatic offer, triggered elsewhere in api/chat.py.
        return None

    normalized = (message or "").strip().lower()
    # Deliberately exclude anything that looks like a DIFFERENT kind of
    # follow-up (availability, order, policy) - this resolver is only
    # for genuine product-preference statements among shown options.
    exclusion_terms = (
        "available", "availability", "stock", "size", "medium", "large",
        "small", "order", "policy", "refund", "return", "checkout",
        "charge", "store", "pickup", "pick up", "ship", "delivery",
    )
    if any(term in normalized for term in exclusion_terms):
        return None

    # Deliberately does NOT use the shared _resolve_context_product's
    # active_product_id fallback here - that fallback is correct for
    # genuine follow-ups ("is it available"), but WRONG for this
    # function's purpose: matching an EXPLICIT name/ordinal reference
    # among several shown products. Confirmed via a real bug: "do you
    # have any shoes?" was incorrectly resolving to a stale
    # active_product_id from an earlier turn, with zero actual
    # relevance check against the message.
    explicit_matches = [
        item for item in selected
        if item.get("name") and _normalize_context_text(item["name"]) in _normalize_context_text(message)
    ]
    if len(explicit_matches) == 1:
        product = explicit_matches[0]
    elif len(explicit_matches) > 1:
        return None
    else:
        product = _resolve_ordinal_reference(message, selected)
    if product is None:
        return None

    structured = StructuredIntent(
        text=f"Selected {product.get('name')} from previous recommendations",
        request_type="recommendation_selection",
        confidence=0.9,
        product_id=product.get("product_id"),
        recommendation_id=product.get("recommendation_id"),
        recommendation_session_id=product.get("recommendation_session_id"),
        extraction_source="deterministic_recommendation_selection",
    )
    _record_context_resolution("recommendation_selection", True, structured)
    return SplitIntentResult(
        sub_intents=[structured.text],
        structured_intent=structured,
        fast_path_used=True,
        llm_splitter_invoked=False,
    )


CART_ADD_REQUEST_RE = re.compile(
    r"(?:can you |could you |please )?add (?:it|this|that|one)(?: (?:to|in) (?:my )?cart)?",
    re.IGNORECASE,
)


def _variant_choice_clarification(context: dict) -> str | None:
    """Stop-and-ask rule: if a cart request refers to a product with
    more than one valid, unresolved variant, ask the customer to choose
    rather than guessing or silently picking one. Confirmed via real,
    live testing before a demo: a size-check showing two genuinely
    different in-stock variants (e.g. white/8 and black/9), followed by
    "add it to my cart", was previously resolving to whichever variant
    happened to be checked LAST, with no indication to the customer
    that a choice was ever made on their behalf.

    Deliberately omits the product name (e.g. "Leather Sneakers") since
    it's already established in conversation - refined per direct
    feedback that repeating it here sounds less natural, more like a
    real salesperson who already knows what's being discussed.
    """
    pending_variants = context.get("pending_variant_choice")
    if not pending_variants or len(pending_variants) < 2:
        return None
    options = []
    for variant in pending_variants:
        color = variant.get("color")
        size = variant.get("size")

        if color and size:
            options.append(f"the {color} one in size {size}")
        elif color:
            options.append(f"the {color} one")
        elif size:
            options.append(f"the one in size {size}")
        else:
            options.append("that option")

    options_text = " or ".join(options)
    return f"Sure — would you like {options_text}?"


SIZE_RECALL_RE = re.compile(
    r"\bwhich (?:size|color|colour)\b.*\b(?:add|added|order|pick)\b|"
    r"\bwhat size\b.*\b(?:add|added)\b",
    re.IGNORECASE,
)


def _resolve_completed_cart_add_recall(message: str, context: dict) -> str | None:
    """Answers a genuine, honest follow-up like "which size did you add?"
    using the REAL, actual result of the most recent, successful cart-add
    - never guessing or claiming not to know when the real answer is
    genuinely available in context. Confirmed via live testing: a
    customer asking this right after a successful add deserves a real,
    correct answer, not "I don't have visibility."
    """
    if not SIZE_RECALL_RE.search(message or ""):
        return None
    completed = context.get("completed_cart_add")
    if not completed or not completed.get("success"):
        return None
    from scout.agents.rendering import _natural_size
    bits = []
    if completed.get("color"):
        bits.append(completed["color"])
    natural_size = _natural_size(completed.get("size"))
    if natural_size:
        bits.append(natural_size)
    if not bits:
        return None
    return ", ".join(bits).capitalize() + "."


def _resolve_size_answer_after_selection(message: str, context: dict) -> str | None:
    """Detects a genuine answer ("medium", "size 8") to the "what size
    would you like?" question asked right after a name/ordinal product
    selection (see supervisor.py's recommendation_selection handling).
    Validates the requested size against REAL, live stock before
    creating the pending_cart_offer - never assumes the customer's
    stated size is genuinely available. Confirmed via live testing: the
    correct flow is always ask -> customer states a size -> confirm
    genuine availability -> THEN offer to add, never skipping the ask
    step even when only one size exists.
    """
    active_product_id = context.get("active_product_id")
    active_product_name = context.get("active_product_name")
    if not active_product_id or not active_product_name:
        return None
    if context.get("pending_cart_offer") or context.get("pending_variant_choice"):
        return None
    normalized = (message or "").strip().strip(".!?")
    # Matches both short size codes (M, L, XL, 8, 9) and full natural-
    # language words (medium, large, small) - the earlier {1,4} length
    # limit only matched short codes, missing genuine, common answers
    # like "Medium" (6 characters). Confirmed via live testing.
    size_match = re.fullmatch(
        r"(?:size\s+)?([A-Za-z0-9]{1,4}|extra small|small|medium|large|extra large|double extra large)",
        normalized,
        re.IGNORECASE,
    )
    if not size_match:
        return None
    from scout.agents.rendering import SIZE_NAMES
    matched_text = size_match.group(1).strip().lower()
    reverse_size_names = {v.lower(): k for k, v in SIZE_NAMES.items()}
    requested_size = reverse_size_names.get(matched_text, matched_text.upper())
    # Real bug fix, found via live testing before a demo: the product's
    # own name often implies a specific color (e.g. "Black Midi Dress"
    # genuinely means black, not any color) - checking the top-level
    # in_stock flag alone is misleading, since it's True if ANY color
    # has that size in stock, even if the color the customer actually
    # means does not. Now matches the specific variant (size AND the
    # name-implied color, if any) within the real, live stock data.
    implied_color = None
    for known_color in ("black", "white", "blue", "red", "floral", "navy", "gray", "grey", "green", "beige", "brown", "pink"):
        if known_color in active_product_name.lower():
            implied_color = known_color
            break
    try:
        from scout.mcp_server import server as local_tools
        result = local_tools.stock(product_id=active_product_id, size=requested_size, color=implied_color or "")
    except Exception:
        return None
    variants = result.get("variants") if isinstance(result, dict) else None
    matched_variant = None
    if isinstance(variants, list):
        for v in variants:
            if not isinstance(v, dict):
                continue
            if str(v.get("size", "")).upper() != requested_size:
                continue
            if implied_color and str(v.get("color", "")).lower() != implied_color:
                continue
            matched_variant = v
            break
    is_genuinely_in_stock = bool(matched_variant and matched_variant.get("in_stock"))
    if not is_genuinely_in_stock:
        return f"That size doesn't look right for the {active_product_name} — could you double check and try again?"
    from scout.agents.rendering import _natural_size
    natural_size = _natural_size(requested_size) or requested_size
    recommendation_id = None
    recommendation_session_id = None

    # Preserve attribution from the exact recommendation that originally
    # produced this product. Variant selection must not erase it.
    for product in context.get("active_selected_products") or []:
        if product.get("product_id") == active_product_id:
            recommendation_id = product.get("recommendation_id")
            recommendation_session_id = product.get(
                "recommendation_session_id"
            )
            break

    context["pending_cart_offer"] = {
        "product_id": active_product_id,
        "product_name": active_product_name,
        "size": requested_size,
        "color": matched_variant.get("color"),
        "quantity": 1,
        "recommendation_id": recommendation_id,
        "recommendation_session_id": recommendation_session_id,
    }
    return f"{natural_size.capitalize()} is available. Want me to add the {active_product_name} in {natural_size} to your cart?"


def _resolve_variant_choice_answer(message: str, context: dict) -> str | None:
    """Detects a genuine answer to the variant-choice clarifying question
    we just asked (e.g. "size 8", "the black one", "white") and sets the
    normal pending_cart_offer for that SPECIFIC, now-unambiguous variant,
    generating the standard "want me to add X?" confirmation - reusing
    the exact same, already-tested confirmation mechanism rather than
    inventing a new one.
    """
    pending_variants = context.get("pending_variant_choice")
    if not pending_variants or len(pending_variants) < 2:
        return None
    normalized = (message or "").strip().strip(".!?").lower()
    matches = []
    for variant in pending_variants:
        size = str(variant.get("size") or "").lower()
        color = str(variant.get("color") or "").lower()
        if (size and size in normalized) or (color and color in normalized):
            matches.append(variant)
    if len(matches) != 1:
        return None
    chosen = matches[0]
    context["pending_variant_choice"] = None
    context["pending_cart_offer"] = {
        "product_id": chosen["product_id"],
        "product_name": chosen["product_name"],
        "size": chosen.get("size"),
        "color": chosen.get("color"),
        "quantity": 1,
        "recommendation_id": chosen.get("recommendation_id"),
        "recommendation_session_id": chosen.get("recommendation_session_id"),
    }
    # Color goes right before the product name ("the white Leather
    # Sneakers"), size goes after ("in size 8") - refined per direct
    # feedback for a more natural, salesperson-like phrasing.
    color_prefix = f"{chosen['color']} " if chosen.get("color") else ""
    size_suffix = f" in size {chosen['size']}" if chosen.get("size") else ""
    return f"Got it — want me to add the {color_prefix}{chosen['product_name']}{size_suffix} to your cart?"


def _resolve_follow_up_intent(message: str, context: dict) -> StructuredIntent | str | None:
    base_intent = classify_clear_single_intent(message)
    normalized = (message or "").lower()
    if CART_ADD_REQUEST_RE.fullmatch((message or "").strip().strip(".!?")):
        clarification = _variant_choice_clarification(context)
        if clarification:
            return clarification
    cart_recall = _resolve_completed_cart_add_recall(message, context)
    if cart_recall:
        return cart_recall
    size_answer = _resolve_size_answer_after_selection(message, context)
    if size_answer:
        return size_answer
    variant_answer = _resolve_variant_choice_answer(message, context)
    if variant_answer:
        return variant_answer
    order_follow_up = _resolve_order_follow_up_intent(message, context, base_intent)
    if order_follow_up is not None:
        return order_follow_up
    is_follow_up = bool(FOLLOW_UP_PRODUCT_RE.search(normalized))
    wants_similar_products = _is_scout_similar_products_request(normalized)
    # Only even consult the recovery-action keyword matcher when there's
    # genuine, recent out-of-stock context to recover from - otherwise
    # its broad keyword regexes (e.g. "today" matching RECOVERY_URGENCY_RE)
    # can misfire on completely unrelated messages. Confirmed via a real
    # bug: "What is the weather today?" was misclassified as a
    # store-availability recovery request purely because it contains
    # the word "today", with zero actual out-of-stock context.
    recovery_action = _out_of_stock_recovery_action(normalized) if context.get("last_out_of_stock_product_id") else None
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

    parsed_size = _parse_requested_size(message) or _parse_contextual_size_reference(message, context)
    parsed_color = _parse_requested_color(message)
    explicit_size = (base_intent.size if base_intent else None) or parsed_size
    explicit_color = (base_intent.color if base_intent else None) or parsed_color
    size = explicit_size or context.get("requested_size")
    color = explicit_color or context.get("requested_color")
    location = (base_intent.location if base_intent else None) or _parse_requested_store_name(message)
    budget_max = (base_intent.budget_max if base_intent else None) or _parse_max_budget(message) or context.get("requested_budget_max")
    if recovery_action == "best_fulfillment":
        recovery_action = "store_availability" if location or context.get("requested_store") else "delivery"
    if recovery_action == "similar_products":
        wants_similar_products = True
    if wants_similar_products:
        if _should_relax_stale_variant_for_similar_search(
            normalized,
            context,
        ):
            if not parsed_size:
                size = None
            if not parsed_color:
                color = None
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
        if "cheaper" in normalized or "less expensive" in normalized:
            text += " with cheaper options"
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
        context["pending_store_availability_product_id"] = product["product_id"]
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


def _resolve_inventory_product_switch_follow_up(message: str, context: dict) -> StructuredIntent | None:
    """Handles turns like "how about Black Midi Dress" while the customer
    is in an availability thread. In that context, a named product is a
    request to check the same variant/fulfillment constraints for a
    different product, not a purchase/cart selection.
    """
    normalized = (message or "").lower()
    if not any(phrase in normalized for phrase in ("how about", "what about")):
        return None
    if not _has_active_inventory_context(context):
        return None

    selected = [
        item
        for item in context.get("active_selected_products") or []
        if isinstance(item, dict) and item.get("product_id") and item.get("name")
    ]
    matches = [
        item
        for item in selected
        if _normalize_context_text(item["name"]) in _normalize_context_text(message)
    ]
    if len(matches) != 1:
        return None

    product = matches[0]
    size = context.get("requested_size")
    color = context.get("requested_color")
    location = _parse_requested_store_name(message) or context.get("requested_store")
    wants_delivery = any(term in normalized for term in ("online", "delivery", "shipping", "ship"))
    wants_store = any(term in normalized for term in ("store", "nearby", "pickup", "pick up")) or bool(location)
    request_type = "store_availability" if wants_store else "inventory_availability"
    if wants_delivery:
        request_type = "inventory_availability"

    product_name = product.get("name") or product["product_id"]
    if request_type == "store_availability":
        text = f"Check pickup availability for product_id {product['product_id']} ({product_name})"
        if location:
            text += f" at {location}"
    elif wants_delivery:
        text = f"Check online or delivery availability for product_id {product['product_id']} ({product_name})"
    else:
        text = f"Check stock for product_id {product['product_id']} ({product_name})"
    if size:
        text += f" in size {size}"
    if color:
        text += f" color {color}"
    text += "."

    context["active_product_id"] = product["product_id"]
    context["active_product_name"] = product_name
    structured = StructuredIntent(
        text=text,
        request_type=request_type,
        confidence=0.96,
        product_type=context.get("active_category"),
        size=size,
        color=color,
        location=location if request_type == "store_availability" else None,
        product_id=product["product_id"],
        fulfillment_preference="delivery" if wants_delivery else "pickup" if request_type == "store_availability" else None,
        extraction_source="deterministic_inventory_product_switch",
    )
    _record_context_resolution("inventory_product_switch_follow_up", True, structured)
    return structured


def _has_active_inventory_context(context: dict) -> bool:
    return bool(
        context.get("requested_size")
        or context.get("requested_color")
        or context.get("requested_store")
        or context.get("pending_store_availability_product_id")
        or context.get("last_out_of_stock_product_id")
    )


def _resolve_pending_store_location_follow_up(message: str, context: dict) -> StructuredIntent | None:
    pending_product_id = context.get("pending_store_availability_product_id")
    if not pending_product_id:
        return None

    location = _parse_requested_store_name(message) or _parse_zip_location(message) or (message or "").strip()
    if not location:
        return None

    product = _resolve_context_product(message, context)
    if product is None or product == "ambiguous":
        for candidate in context.get("active_selected_products") or []:
            if candidate.get("product_id") == pending_product_id:
                product = candidate
                break
    if not isinstance(product, dict):
        return None

    size = context.get("requested_size")
    color = context.get("requested_color")
    product_name = product.get("name") or context.get("active_product_name") or product["product_id"]
    text = f"Check pickup availability for product_id {product['product_id']} ({product_name}) at {location}"
    if size:
        text += f" in size {size}"
    if color:
        text += f" color {color}"
    text += "."

    context["requested_store"] = location
    context["pending_store_availability_product_id"] = None
    structured = StructuredIntent(
        text=text,
        request_type="store_availability",
        confidence=0.96,
        product_type=context.get("active_category"),
        size=size,
        color=color,
        location=location,
        product_id=product["product_id"],
        fulfillment_preference="pickup",
        extraction_source="deterministic_store_location_follow_up",
    )
    _record_context_resolution("pending_store_location_follow_up", True, structured)
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
    # Distinguished from plain status HERE, at the moment the intent is
    # created - so this real, semantic difference (shipping/tracking vs.
    # generic order status) survives downstream as request_type itself,
    # rather than needing every later consumer to re-derive it from
    # text that may have already been rewritten into a synthetic phrase.
    wants_shipping = any(
        term in normalized
        for term in ("where", "track", "tracking", "arrive", "arrival", "shipped", "shipment", "when will")
    )
    wants_status = wants_shipping or any(
        term in normalized
        for term in (
            "status",
            "when",
            "what did i order",
            "what's in it",
            "what is in it",
            "items",
        )
    )
    if not wants_return and not wants_status:
        return None

    if wants_return:
        request_type = "return_eligibility"
    elif wants_shipping:
        request_type = "shipment_status"
    else:
        request_type = "order_status"
    action = {
        "return_eligibility": "Check return eligibility",
        "shipment_status": "Check shipment status",
        "order_status": "Check order status",
    }[request_type]
    structured = StructuredIntent(
        text=f"{action} for order {active_order_id}.",
        request_type=request_type,
        confidence=0.96,
        order_id=active_order_id,
        extraction_source="deterministic_order_context",
    )
    _record_context_resolution("order_follow_up_resolution", True, structured)
    return structured


ORDINAL_WORDS = {
    "first": 0, "1st": 0, "one": 0,
    "second": 1, "2nd": 1, "two": 1,
    "third": 2, "3rd": 2, "three": 2,
    "fourth": 3, "4th": 3, "four": 3,
    "fifth": 4, "5th": 4, "five": 4,
    "last": -1,
}


# Requires the ordinal word to appear in a genuine SELECTION shape -
# "the second one", "the third dress", "number two" - not just anywhere
# in the message. Confirmed via a real bug: plain word-matching let
# "first" in "is it available in medium first?" (meaning "before we
# continue", not "item #1") incorrectly resolve to the first product in
# the list, silently answering about the wrong item entirely.
ORDINAL_SELECTION_RE = re.compile(
    r"\b(?:the\s+)?(first|1st|second|2nd|third|3rd|fourth|4th|fifth|5th|last)\s+(?:one|dress|item|option|product)\b"
    r"|\bnumber\s+(one|two|three|four|five|1|2|3|4|5)\b",
    re.IGNORECASE,
)


def _resolve_ordinal_reference(message: str, selected: list[dict]) -> dict | None:
    """Matches phrases like 'the second one', 'the second dress', 'the
    last option' against the exact order products were actually shown in
    (active_selected_products, which preserves display order - see
    _update_context_from_verified_turn in conversation.py). Deliberately
    requires a genuine selection-shaped phrase (see ORDINAL_SELECTION_RE),
    not just any occurrence of an ordinal word - an incorrect ordinal
    match here could lead to offering to add the WRONG product to cart,
    which is a real, meaningful mistake to avoid.
    """
    if not selected:
        return None
    normalized = _normalize_context_text(message)
    match = ORDINAL_SELECTION_RE.search(normalized)
    if not match:
        return None
    word = (match.group(1) or match.group(2) or "").lower()
    if word in ORDINAL_WORDS:
        index = ORDINAL_WORDS[word]
        try:
            return selected[index]
        except IndexError:
            return None
    return None


def _safe_rating(value) -> float | None:
    try:
        rating = float(value)
    except (TypeError, ValueError):
        return None
    return rating if rating > 0 else None


def _resolve_rating_comparison_follow_up(message: str, context: dict) -> StructuredIntent | None:
    normalized = (message or "").lower()
    if not (
        ("rated" in normalized or "rating" in normalized)
        and any(term in normalized for term in ("which", "better", "highest", "these", "them"))
    ):
        return None
    products = [
        product
        for product in context.get("active_selected_products") or []
        if isinstance(product, dict) and product.get("product_id") and _safe_rating(product.get("rating")) is not None
    ]
    if len(products) < 2:
        return None
    best = max(products, key=lambda product: (_safe_rating(product.get("rating")) or 0, product.get("name") or ""))
    best_name = best.get("name") or best["product_id"]
    best_rating = _safe_rating(best.get("rating"))
    compared = [
        f"{product.get('name') or product['product_id']} is rated {_safe_rating(product.get('rating')):.1f}"
        for product in products[:3]
    ]
    reply = f"{best_name} is better rated at {best_rating:.1f}. " + "; ".join(compared) + "."
    return StructuredIntent(
        text=reply,
        request_type="rating_comparison",
        confidence=0.98,
        extraction_source="deterministic_rating_comparison",
    )


def _safe_price(value) -> float | None:
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return price if price >= 0 else None


def _sale_price(product: dict) -> float | None:
    promotion = product.get("promotion")
    if isinstance(promotion, dict):
        return _safe_price(promotion.get("discounted_price"))
    return None


def _effective_price(product: dict) -> float | None:
    return _sale_price(product) or _safe_price(product.get("price"))


def _format_money(value: float | None) -> str:
    return f"${value:.2f}" if value is not None else "the listed price"


def _selected_recommendation_products(context: dict) -> list[dict]:
    return [
        product
        for product in context.get("active_selected_products") or []
        if isinstance(product, dict) and (product.get("product_id") or product.get("external_product_id"))
    ]


def _product_key(product: dict) -> str:
    return product.get("product_id") or product.get("external_product_id") or "this item"


def _product_name(product: dict) -> str:
    return product.get("name") or _product_key(product)


def _compare_current_recommendations_reply(products: list[dict]) -> str:
    if len(products) < 2:
        return "I can compare them — which other item should I use?"
    first, second = products[:2]
    first_name = _product_name(first)
    second_name = _product_name(second)
    first_rating = _safe_rating(first.get("rating"))
    second_rating = _safe_rating(second.get("rating"))
    first_price = _effective_price(first)
    second_price = _effective_price(second)
    if first.get("source") == "external" or second.get("source") == "external":
        first_vendor = f" from {first['vendor_name']}" if first.get("vendor_name") else ""
        second_vendor = f" from {second['vendor_name']}" if second.get("vendor_name") else ""
        cheaper = None
        if first_price is not None and second_price is not None:
            cheaper = first if first_price <= second_price else second
        reply = (
            f"Of course — here’s the quick comparison: {first_name}{first_vendor} is {_format_money(first_price)}, "
            f"and {second_name}{second_vendor} is {_format_money(second_price)}."
        )
        if cheaper:
            reply += f" {_product_name(cheaper)} is the lower-priced option."
        reply += " Since these are vendor partners, please confirm sizing, shipping, and returns on their site before buying."
        return reply
    lines = [
        f"Of course — here’s the quick version: {first_name} is {_format_money(first_price)}, and {second_name} is {_format_money(second_price)}."
    ]
    if first_rating is not None and second_rating is not None:
        better = first if first_rating >= second_rating else second
        lines.append(f"{_product_name(better)} is better rated at {max(first_rating, second_rating):.1f}.")
    if first_price is not None and second_price is not None:
        cheaper = first if first_price <= second_price else second
        lines.append(f"{_product_name(cheaper)} is the cheaper pick.")
    return " ".join(lines)


def _cheaper_current_recommendations_reply(products: list[dict]) -> str | None:
    priced = [product for product in products if _effective_price(product) is not None]
    if len(priced) < 2:
        return None
    cheapest = min(priced, key=lambda product: _effective_price(product) or 10_000)
    price = _safe_price(cheapest.get("price"))
    sale = _sale_price(cheapest)
    price_text = _format_money(price)
    if sale is not None and price is not None and sale < price:
        price_text = f"{price_text}, with a sale price of {_format_money(sale)}"
    if cheapest.get("source") == "external":
        vendor = f" from {cheapest['vendor_name']}" if cheapest.get("vendor_name") else ""
        return (
            f"Good question — {_product_name(cheapest)}{vendor} is the lowest-priced vendor partner option I’m showing at {price_text}. "
            "Because it’s from another retailer, please confirm the final price and availability on their site."
        )
    return (
        f"Good question — {_product_name(cheapest)} is already the cheapest of these at {price_text}. "
        "I don’t see a cheaper similar Scout option in this set, but I can look with a lower budget if you want."
    )


def _resolve_recommendation_action_follow_up(message: str, context: dict) -> StructuredIntent | None:
    normalized = (message or "").lower()
    products = _selected_recommendation_products(context)
    complex_follow_up = " and " in normalized and any(
        term in normalized
        for term in ("check", "store", "stores", "available", "availability", "pickup", "pick up")
    )
    if len(products) >= 2 and "compare" in normalized and not complex_follow_up:
        return StructuredIntent(
            text=_compare_current_recommendations_reply(products),
            request_type="recommendation_action_reply",
            confidence=0.98,
            extraction_source="deterministic_recommendation_action",
        )
    if "cheaper" in normalized and "similar" in normalized:
        reply = _cheaper_current_recommendations_reply(products)
        if reply:
            return StructuredIntent(
                text=reply,
                request_type="recommendation_action_reply",
                confidence=0.98,
                extraction_source="deterministic_recommendation_action",
            )
    if "scout catalog options only" in normalized or "lumi picks" in normalized:
        if any(product.get("source") == "external" for product in products):
            product_type = context.get("active_category") or "items"
            color = context.get("requested_color")
            budget = context.get("requested_budget_max")
            description = "Lumi"
            if color:
                description += f" {color}"
            description += f" {product_type}"
            if budget is not None:
                description += f" under ${float(budget):g}"
            return StructuredIntent(
                text=(
                    f"I checked Lumi’s own catalog for {description}, but I don’t see a matching item right now. "
                    "The vendor partner options above are separate retailer offers, so you would complete those purchases on their sites."
                ),
                request_type="recommendation_action_reply",
                confidence=0.98,
                extraction_source="deterministic_external_catalog_only_followup",
            )
        product_type = context.get("active_category") or "products"
        color = context.get("requested_color")
        budget = context.get("requested_budget_max")
        text = "Show Scout catalog options only"
        if color:
            text += f" for {color}"
        text += f" {product_type}"
        if budget is not None:
            text += f" under ${float(budget):g}"
        return StructuredIntent(
            text=text,
            request_type="product_recommendation",
            confidence=0.94,
            product_type=product_type,
            budget_max=float(budget) if budget is not None else None,
            color=color,
            extraction_source="deterministic_catalog_only_followup",
        )
    return None


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
    ordinal_match = _resolve_ordinal_reference(message, selected)
    if ordinal_match is not None:
        return ordinal_match
    # A pending cart offer represents the customer's most recent,
    # specific focus - it takes precedence over the more general
    # active_product_id, which can otherwise drift stale (e.g. still
    # pointing at the FIRST product from a list, even after the
    # customer explicitly selected a different one). Confirmed via a
    # real, live-tested conversation: "I like the second one" -> "is it
    # available in medium?" incorrectly checked the first product shown,
    # not the one the customer had just selected and was awaiting
    # confirmation on.
    pending_offer = context.get("pending_cart_offer")
    if pending_offer and pending_offer.get("product_id"):
        return {
            "product_id": pending_offer["product_id"],
            "name": pending_offer.get("product_name"),
        }
    active_id = context.get("active_product_id")
    if active_id:
        for item in selected:
            if item.get("product_id") == active_id:
                return item
        return {"product_id": active_id, "name": context.get("active_product_name")}
    if len(selected) == 1 and FOLLOW_UP_PRODUCT_RE.search(message or ""):
        return selected[0]
    return None


def _should_relax_stale_variant_for_similar_search(
    normalized_message: str,
    context: dict,
) -> bool:
    if not context.get("last_out_of_stock_product_id"):
        return False
    if not (context.get("requested_size") or context.get("requested_color")):
        return False
    recovery_terms = (
        "similar",
        "something else",
        "anything else",
        "other option",
        "other options",
        "another option",
        "cheaper",
        "alternative",
        "alternatives",
        "like this",
        "like it",
    )
    return any(term in normalized_message for term in recovery_terms)


def _parse_contextual_size_reference(customer_message: str, context: dict) -> str | None:
    if not context.get("requested_size"):
        return None
    lower = (customer_message or "").lower()
    if re.search(r"\b(?:that|same|this)\s+size\b|\bin\s+that\s+size\b", lower):
        return context.get("requested_size")
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


def _parse_zip_location(customer_message: str) -> str | None:
    match = re.fullmatch(r"\s*(\d{5})(?:-\d{4})?\s*", customer_message or "")
    return match.group(1) if match else None


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
