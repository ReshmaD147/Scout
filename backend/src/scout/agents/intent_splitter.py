import json
import re
import time
from dataclasses import dataclass

from scout.agents.diagnostics import get_diagnostics, record_model_call
from scout.agents.model_provider import get_chat_model, with_no_think

SPLIT_PROMPT = with_no_think("""...""")  # unchanged
MERGE_PROMPT_TEMPLATE = with_no_think("""...""")  # unchanged


@dataclass(frozen=True)
class StructuredIntent:
    text: str
    request_type: str
    confidence: float
    extraction_source: str = "deterministic_fast_path"
    product_type: str | None = None
    budget_max: float | None = None
    size: str | None = None
    color: str | None = None
    location: str | None = None
    order_id: str | None = None
    product_id: str | None = None
    recommendation_id: str | None = None
    fulfillment_preference: str | None = None
    needs_clarification: bool = False
    clarification_question: str | None = None


@dataclass(frozen=True)
class PlannedSubIntent:
    intent: str
    specialist: str
    text: str
    depends_on: list[str]
    conditional: str | None = None


@dataclass(frozen=True)
class ExecutionPlan:
    multi_intent: bool
    subgoals: list[PlannedSubIntent]
    budget_max: float | None = None
    requested_size: str | None = None
    requested_color: str | None = None
    requested_store: str | None = None
    product_type: str | None = None


@dataclass(frozen=True)
class SplitIntentResult:
    sub_intents: list[str]
    structured_intent: StructuredIntent | None = None
    fast_path_used: bool = False
    llm_splitter_invoked: bool = False
    execution_plan: ExecutionPlan | None = None


_PRODUCT_TERMS = {
    "dress",
    "dresses",
    "shoe",
    "shoes",
    "shirt",
    "shirts",
    "jeans",
    "jacket",
    "jackets",
    "coat",
    "coats",
    "pants",
    "skirt",
    "skirts",
    "sweater",
    "sweaters",
    "top",
    "tops",
    "bag",
    "bags",
}
_COLOR_TERMS = {
    "black",
    "white",
    "red",
    "blue",
    "green",
    "yellow",
    "pink",
    "purple",
    "brown",
    "gray",
    "grey",
    "navy",
    "cream",
    "beige",
}
_SIZE_TERMS = {
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
}
_SIZE_PATTERN = re.compile(
    r"\b(?:in\s+(?:a\s+)?|available\s+in\s+|do\s+you\s+have\s+(?:this|it)?\s*in\s+|size\s+)"
    r"(extra small|extra large|xs|small|medium|large|xl|xxl|s|m|l)\b|"
    r"\b(extra small|extra large|xs|small|medium|large|xl|xxl|s|m|l)\s+(?:size|availability)\b",
    re.IGNORECASE,
)
_BUDGET_PATTERN = re.compile(
    r"\b(?:under|below|max(?:imum)?|no more than|less than|up to)\s*\$?(\d+(?:\.\d{1,2})?)\b",
    re.IGNORECASE,
)
_ORDER_PATTERN = re.compile(r"\b(?:ORD[-\s]?\d{3,}|O\d{3,}|\d{3,})\b", re.IGNORECASE)
_STORE_PATTERN = re.compile(
    r"\b(?:at|in|near)\s+([A-Z][A-Za-z]*(?:\s+[A-Z][A-Za-z]*){0,3})\b"
)
_AMBIGUOUS_FOLLOW_UPS = {
    "what about that one",
    "what about this one",
    "do everything we discussed",
    "which one is better",
    "what about it",
}
_GREETING_RE = re.compile(r"^\s*(hi|hello|hey|good morning|good afternoon|good evening)[!.]?\s*$", re.IGNORECASE)
_THANKS_RE = re.compile(r"^\s*(thanks|thank you|thx|that's all|that is all|bye|goodbye)[!.]?\s*$", re.IGNORECASE)
_PURCHASE_EXECUTION_RE = re.compile(
    r"\b(?:buy|purchase|charge|pay|checkout|check out|place (?:the )?order|complete (?:the )?checkout)\b",
    re.IGNORECASE,
)
_SHOPPING_REQUEST_RE = re.compile(r"\b(?:need|looking for|find|show me|recommend|something)\b", re.IGNORECASE)
_OUT_OF_SCOPE_RE = re.compile(
    r"\b(?:weather|temperature|forecast|rain|snow|traffic|news|sports score|stock price|"
    r"medical advice|legal advice|recipe|homework)\b",
    re.IGNORECASE,
)


def classify_clear_single_intent(message: str) -> StructuredIntent | None:
    if not message or not message.strip():
        return None

    original = message.strip()
    lowered = original.lower()
    normalized = re.sub(r"\s+", " ", lowered).strip(" .!?")

    if any(phrase in normalized for phrase in _AMBIGUOUS_FOLLOW_UPS):
        return None
    if _has_conflicting_instruction(normalized):
        return None
    if _looks_like_complex_comparison(normalized):
        return None
    if _looks_multi_intent(normalized):
        return None

    if _GREETING_RE.match(original):
        return StructuredIntent(text=original, request_type="greeting", confidence=0.99)
    if _THANKS_RE.match(original):
        return StructuredIntent(text=original, request_type="thanks", confidence=0.99)
    if _is_out_of_scope_request(normalized):
        return StructuredIntent(text=original, request_type="out_of_scope", confidence=0.95)

    if _is_purchase_execution_request(normalized):
        return StructuredIntent(text=original, request_type="purchase_execution", confidence=0.96)

    order_id = _extract_order_id(original)
    if order_id and _contains_any(normalized, {"return", "refund", "exchange", "eligible", "eligibility"}):
        return StructuredIntent(
            text=original,
            request_type="return_eligibility",
            order_id=order_id,
            confidence=0.95,
        )
    if order_id and _contains_any(normalized, {"where", "track", "tracking", "shipped", "shipment", "arrive", "arrival"}):
        return StructuredIntent(
            text=original,
            request_type="shipment_status",
            order_id=order_id,
            confidence=0.95,
        )
    if order_id and _contains_any(normalized, {"status", "order"}):
        return StructuredIntent(
            text=original,
            request_type="order_status",
            order_id=order_id,
            confidence=0.95,
        )

    product_type = _extract_product_type(normalized)
    budget_max = _extract_budget_max(original)
    size = _extract_size(original)
    color = _extract_color(normalized)
    location = _extract_location(original, normalized)

    if _is_vague_shopping_request(normalized, product_type, budget_max):
        return StructuredIntent(
            text=original,
            request_type="shopping_clarification",
            confidence=0.92,
            product_type=product_type,
            needs_clarification=True,
            clarification_question=_clarification_question_for(product_type),
        )

    if _contains_any(normalized, {"third-party", "third party", "external", "alternative", "alternatives"}) and (
        _contains_any(normalized, {"show", "find", "search", "offer", "offers"}) or product_type
    ):
        return StructuredIntent(
            text=original,
            request_type="external_offer",
            product_type=product_type,
            budget_max=budget_max,
            color=color,
            confidence=0.9,
        )

    if _contains_any(normalized, {"policy", "policies", "refund", "return", "exchange", "shipping"}) and not product_type:
        return StructuredIntent(text=original, request_type="policy_question", confidence=0.9)

    if _is_clear_size_availability(normalized, product_type, size):
        return StructuredIntent(
            text=original,
            request_type="inventory_availability",
            product_type=product_type,
            size=size,
            color=color,
            confidence=0.9,
        )

    if _contains_any(normalized, {"available", "availability", "pickup", "pick up", "store", "nearby"}) and (
        product_type or location
    ):
        return StructuredIntent(
            text=original,
            request_type="store_availability" if location else "inventory_availability",
            product_type=product_type,
            size=size,
            color=color,
            location=location,
            fulfillment_preference="pickup" if _contains_any(normalized, {"pickup", "pick up"}) else None,
            confidence=0.88,
        )

    if _contains_any(normalized, {"in stock", "stock", "available", "availability", "size"}) and (product_type or size or color):
        return StructuredIntent(
            text=original,
            request_type="inventory_availability",
            product_type=product_type,
            size=size,
            color=color,
            confidence=0.88,
        )

    if _contains_any(normalized, {"recommend", "find", "show me", "search", "looking for", "need", "do you have", "have any"}) and product_type:
        return StructuredIntent(
            text=original,
            request_type="product_recommendation",
            product_type=product_type,
            budget_max=budget_max,
            size=size,
            color=color,
            confidence=0.9,
        )

    return None


def split_intents(model, message: str) -> list[str]:
    return split_intents_with_metadata(model, message).sub_intents


def split_intents_with_metadata(model, message: str) -> SplitIntentResult:
    """Splits a message into sub-intents. Falls back to treating the
    whole message as one intent if the model call fails OUTRIGHT (API
    error, empty input) or if its output can't be parsed as a valid JSON
    list — never lets a splitter failure block the conversation."""
    if not message or not message.strip():
        return SplitIntentResult(sub_intents=[message])

    classifier_started = time.perf_counter()
    execution_plan = detect_supported_compound_intent(message)
    if execution_plan is not None:
        _record_splitter_decision(
            fast_path_used=True,
            selected_intent_type="multi_intent",
            classification_duration_ms=(time.perf_counter() - classifier_started) * 1000,
            llm_splitter_invoked=False,
        )
        return SplitIntentResult(
            sub_intents=[subgoal.text for subgoal in execution_plan.subgoals],
            fast_path_used=True,
            llm_splitter_invoked=False,
            execution_plan=execution_plan,
        )

    structured_intent = classify_clear_single_intent(message)
    _record_splitter_decision(
        fast_path_used=structured_intent is not None,
        selected_intent_type=structured_intent.request_type if structured_intent else None,
        classification_duration_ms=(time.perf_counter() - classifier_started) * 1000,
        llm_splitter_invoked=structured_intent is None,
    )
    if structured_intent is not None:
        return SplitIntentResult(
            sub_intents=[structured_intent.text],
            structured_intent=structured_intent,
            fast_path_used=True,
            llm_splitter_invoked=False,
        )

    try:
        llm_started = time.perf_counter()
        response = model.invoke([
            {"role": "system", "content": SPLIT_PROMPT},
            {"role": "user", "content": message},
        ])
        record_model_call(
            stage="intent_splitter_model_invocation",
            elapsed_ms=(time.perf_counter() - llm_started) * 1000,
            message_count=2,
            input_chars=len(message),
        )
    except Exception:
        return SplitIntentResult(sub_intents=[message], llm_splitter_invoked=True)

    text = response.content if isinstance(response.content, str) else str(response.content)

    try:
        parsed = json.loads(text.strip())
        if isinstance(parsed, list) and all(isinstance(x, str) for x in parsed) and parsed:
            return SplitIntentResult(sub_intents=parsed, llm_splitter_invoked=True)
    except (json.JSONDecodeError, TypeError):
        pass

    return SplitIntentResult(sub_intents=[message], llm_splitter_invoked=True)


def detect_supported_compound_intent(message: str) -> ExecutionPlan | None:
    if not message or not message.strip():
        return None

    original = message.strip()
    normalized = re.sub(r"\s+", " ", original.lower()).strip(" .!?")
    if not re.search(r"\b(and|also|plus|then|if)\b", normalized):
        return None
    if _has_conflicting_instruction(normalized):
        return None

    product_type = _extract_product_type(normalized)
    budget_max = _extract_budget_max(original)
    size = _extract_size(original)
    color = _extract_color(normalized)
    location = _extract_location_for_compound(original, normalized)
    order_id = _extract_order_id(original)

    has_comparison = _looks_like_complex_comparison(normalized)
    has_recommendation = bool(
        product_type
        and _contains_any(normalized, {"recommend", "find", "show me", "search", "looking for", "need", "do you have", "have any", "compare", "pick", "rank"})
    )
    has_inventory = bool(
        _contains_any(normalized, {"available", "availability", "stock", "in stock", "size", "medium", "large", "small"})
        and (has_recommendation or product_type)
    )
    has_store = bool(location or _contains_any(normalized, {"pickup", "pick up", "store"}))
    has_policy = _contains_any(normalized, {"policy", "return policy", "refund policy", "shipping policy", "returns", "opened-item returns", "opened item returns"}) or bool(
        re.search(r"\breturn (?:it|them|this|after|if|when)\b", normalized)
    )
    has_external_condition = bool(
        has_recommendation
        and _contains_any(normalized, {"outside", "external", "third-party", "third party"})
        and _contains_any(normalized, {"if scout has none", "if you have none", "if none", "fallback", "option"})
    )
    has_order_status = bool(order_id and _contains_any(normalized, {"where", "status", "track", "tracking", "order"}))
    has_return_eligibility = bool(order_id and _contains_any(normalized, {"eligible", "eligibility", "return"}))
    wants_order_policy_explanation = bool(
        has_return_eligibility
        and _contains_any(normalized, {"why", "explain", "because", "reason", "policy", "rule", "rules"})
    )

    subgoals: list[PlannedSubIntent] = []

    if has_order_status and has_return_eligibility:
        subgoals.append(
            PlannedSubIntent(
                intent="order_status",
                specialist="order_agent",
                text=f"Check status for order {order_id}.",
                depends_on=[],
            )
        )
        subgoals.append(
            PlannedSubIntent(
                intent="return_eligibility",
                specialist="order_agent",
                text=f"Check return eligibility for order {order_id}.",
                depends_on=["order_status"],
            )
        )
        if wants_order_policy_explanation:
            subgoals.append(
                PlannedSubIntent(
                    intent="policy_question",
                    specialist="policy_agent",
                    text="Explain the Scout return policy rule for order return eligibility.",
                    depends_on=["return_eligibility"],
                )
            )
        return ExecutionPlan(
            multi_intent=True,
            subgoals=subgoals,
        )

    if not has_recommendation:
        return None

    recommendation_text = _recommendation_sub_intent_text(original, product_type, budget_max, color)
    subgoals.append(
        PlannedSubIntent(
            intent="product_recommendation",
            specialist="recommend_agent",
            text=recommendation_text,
            depends_on=[],
        )
    )

    if has_comparison:
        subgoals.append(
            PlannedSubIntent(
                intent="product_comparison",
                specialist="recommend_agent",
                text=_comparison_sub_intent_text(product_type, budget_max, color),
                depends_on=["product_recommendation"],
            )
        )

    if has_inventory or has_store:
        availability_intent = "store_availability" if has_store else "inventory_availability"
        subgoals.append(
            PlannedSubIntent(
                intent=availability_intent,
                specialist="inventory_agent",
                text=_availability_sub_intent_text(availability_intent, product_type, size, color, location),
                depends_on=["product_recommendation"],
            )
        )

    if has_external_condition:
        subgoals.append(
            PlannedSubIntent(
                intent="external_offer",
                specialist="external_offer_agent",
                text=_external_sub_intent_text(product_type, budget_max, color),
                depends_on=["product_recommendation"],
                conditional="internal_insufficient",
            )
        )

    if has_policy:
        subgoals.append(
            PlannedSubIntent(
                intent="policy_question",
                specialist="policy_agent",
                text=_policy_sub_intent_text(original, has_external_condition),
                depends_on=[],
            )
        )

    if len(subgoals) < 2:
        return None

    return ExecutionPlan(
        multi_intent=True,
        subgoals=subgoals,
        budget_max=budget_max,
        requested_size=size,
        requested_color=color,
        requested_store=location,
        product_type=product_type,
    )


def _recommendation_sub_intent_text(original: str, product_type: str | None, budget_max: float | None, color: str | None) -> str:
    recommendation_clause = re.split(
        r"\b(?:and\s+(?:check|explain|tell|show)|if\s+scout\s+has\s+none|if\s+you\s+have\s+none)\b",
        original,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0].strip(" ,.;")
    if recommendation_clause:
        return recommendation_clause + "."
    parts = ["Recommend"]
    if color:
        parts.append(color)
    parts.append(product_type or "products")
    if budget_max is not None:
        parts.append(f"under ${budget_max:g}")
    return " ".join(parts) + "."


def _availability_sub_intent_text(
    intent: str,
    product_type: str | None,
    size: str | None,
    color: str | None,
    location: str | None,
) -> str:
    parts = ["Check", "store availability" if intent == "store_availability" else "stock", "for selected recommended products"]
    if size:
        parts.append(f"in size {size}")
    if color:
        parts.append(f"in {color}")
    if location:
        parts.append(f"at {location}")
    if product_type:
        parts.append(f"for {product_type}")
    return " ".join(parts) + "."


def _external_sub_intent_text(product_type: str | None, budget_max: float | None, color: str | None) -> str:
    parts = ["Search third-party offers for"]
    if color:
        parts.append(color)
    parts.append(product_type or "products")
    if budget_max is not None:
        parts.append(f"under ${budget_max:g}")
    return " ".join(parts) + "."


def _comparison_sub_intent_text(product_type: str | None, budget_max: float | None, color: str | None) -> str:
    parts = ["Compare verified"]
    if color:
        parts.append(color)
    parts.append(product_type or "products")
    if budget_max is not None:
        parts.append(f"under ${budget_max:g}")
    parts.append("and pick the best option")
    return " ".join(parts) + "."


def _policy_sub_intent_text(original: str, external_limitation: bool) -> str:
    if external_limitation:
        return "Explain that Scout return policy does not establish third-party retailer return rules."
    lowered = original.lower()
    if "opened" in lowered or "opening" in lowered or "after opening" in lowered:
        return "Explain the return policy for opened items."
    if "refund" in lowered:
        return "Explain the refund policy."
    return "Explain the return policy."


def _extract_location_for_compound(original: str, lowered: str) -> str | None:
    match = _STORE_PATTERN.search(original)
    if not match:
        if re.search(r"\bmaple\s+grove\b", lowered):
            return "Maple Grove"
        return None
    candidate = match.group(1).strip()
    if candidate.lower() in _COLOR_TERMS or candidate.lower() in _PRODUCT_TERMS:
        return None
    return candidate


def merge_answers(model, original_message: str, answers: list[str]) -> str:
    """Combines already-finalized sub-intent answers deterministically.
    This step must not call a model because all factual content has
    already been verified and rendered per sub-intent."""
    cleaned_answers = [answer.strip() for answer in answers if answer and answer.strip()]
    if len(cleaned_answers) == 1:
        return cleaned_answers[0]

    order_summary = _merged_order_summary(cleaned_answers)
    if order_summary:
        return order_summary

    return " ".join(_unique_sentences(cleaned_answers))


def _merged_order_summary(answers: list[str]) -> str | None:
    text = " ".join(answers)
    order_match = re.search(r"\b(O\d{3,})\b", text)
    if not order_match:
        return None
    order_id = order_match.group(1)
    status_match = re.search(rf"\bOrder\s+{re.escape(order_id)}\s+(?:is\s+currently\s+([a-z]+)|has\s+(shipped))\b", text, re.IGNORECASE)
    eligible_match = re.search(r"\bIt is (eligible|not eligible) for return\b", text, re.IGNORECASE)
    tracking_match = re.search(rf"\bOrder\s+{re.escape(order_id)}\s+tracking number is ([A-Z0-9-]+)\b", text, re.IGNORECASE)
    payment_match = re.search(rf"\bOrder\s+{re.escape(order_id)}\s+payment status is ([a-z]+)\b", text, re.IGNORECASE)
    if not any((status_match, eligible_match, tracking_match, payment_match)):
        return None

    sentences = []
    if status_match and eligible_match:
        status = (status_match.group(1) or status_match.group(2)).lower()
        status_phrase = "has shipped" if status == "shipped" else f"is currently {status}"
        eligibility = "eligible" if eligible_match.group(1).lower() == "eligible" else "not eligible"
        sentences.append(f"Order {order_id} {status_phrase} and is currently {eligibility} for return.")
    elif status_match:
        status = (status_match.group(1) or status_match.group(2)).lower()
        sentences.append(f"Order {order_id} {'has shipped' if status == 'shipped' else f'is currently {status}'}.")
    elif eligible_match:
        eligibility = "eligible" if eligible_match.group(1).lower() == "eligible" else "not eligible"
        sentences.append(f"Order {order_id} is currently {eligibility} for return.")
    if tracking_match:
        sentences.append(f"Tracking number: {tracking_match.group(1)}.")
    if payment_match:
        sentences.append(f"Payment status is {payment_match.group(1).lower()}.")

    remaining = [
        sentence
        for sentence in _unique_sentences(answers)
        if order_id not in sentence and "eligible for return" not in sentence.lower() and "not eligible for return" not in sentence.lower()
    ]
    return " ".join([*sentences, *remaining])


def _unique_sentences(answers: list[str]) -> list[str]:
    seen = set()
    output = []
    for answer in answers:
        for sentence in re.split(r"(?<=[.!?])\s+", answer):
            cleaned = re.sub(r"^(?:First|Also|Finally|Part\s+\d+):\s*", "", sentence.strip())
            if not cleaned:
                continue
            if _is_redundant_external_policy_limitation(cleaned, output):
                continue
            key = cleaned.lower()
            if key in seen:
                continue
            seen.add(key)
            output.append(cleaned)
    return output


def _is_redundant_external_policy_limitation(sentence: str, prior_sentences: list[str]) -> bool:
    lowered = sentence.lower()
    if "third-party retailer return-policy evidence" not in lowered:
        return False
    return any(
        (
            "scout's return policy does not apply to this outside offer" in prior.lower()
            or "scout’s return policy doesn’t apply" in prior.lower()
        )
        and "verified return-policy information for" in prior.lower()
        for prior in prior_sentences
    )


def _record_splitter_decision(
    *,
    fast_path_used: bool,
    selected_intent_type: str | None,
    classification_duration_ms: float,
    llm_splitter_invoked: bool,
) -> None:
    diagnostics = get_diagnostics()
    if diagnostics is None:
        return
    diagnostics.record(
        "deterministic_intent_classification",
        classification_duration_ms,
        fast_path_used=fast_path_used,
        selected_intent_type=selected_intent_type,
        llm_splitter_invoked=llm_splitter_invoked,
    )


def _contains_any(text: str, terms: set[str]) -> bool:
    return any(term in text for term in terms)


def _extract_budget_max(text: str) -> float | None:
    match = _BUDGET_PATTERN.search(text)
    return float(match.group(1)) if match else None


def _extract_order_id(text: str) -> str | None:
    match = _ORDER_PATTERN.search(text)
    return match.group(0).upper().replace(" ", "-") if match else None


def _extract_size(text: str) -> str | None:
    match = _SIZE_PATTERN.search(text)
    if not match:
        return None
    value = next(group for group in match.groups() if group)
    return _SIZE_TERMS.get(value.lower())


def _is_clear_size_availability(normalized: str, product_type: str | None, size: str | None) -> bool:
    if not size:
        return False
    has_product_reference = bool(product_type) or "this" in normalized or "it" in normalized
    if not has_product_reference:
        return False
    return bool(
        re.search(r"\b(?:is|are|do|does|have|has|available|stock|carry)\b", normalized)
        or re.search(r"\b(?:in\s+(?:a\s+)?|size\s+|available\s+in\s+)", normalized)
    )


def _extract_color(text: str) -> str | None:
    for color in sorted(_COLOR_TERMS, key=len, reverse=True):
        if re.search(rf"\b{re.escape(color)}\b", text):
            return color
    return None


def _extract_product_type(text: str) -> str | None:
    for term in sorted(_PRODUCT_TERMS, key=len, reverse=True):
        if re.search(rf"\b{re.escape(term)}\b", text):
            return term
    return None


def _is_purchase_execution_request(text: str) -> bool:
    if _contains_any(text, {"payment status", "checkout policy", "payment policy", "refund", "return"}):
        return False
    return bool(_PURCHASE_EXECUTION_RE.search(text)) and _contains_any(
        text,
        {"for me", "my card", "charge me", "place the order", "complete checkout", "complete the checkout", "pay for", "buy this", "buy the"},
    )


def _is_out_of_scope_request(text: str) -> bool:
    if not _OUT_OF_SCOPE_RE.search(text):
        return False
    return not (
        _extract_product_type(text)
        or _contains_any(text, {"order", "return", "refund", "shipping", "exchange", "checkout", "payment"})
    )


def _is_vague_shopping_request(text: str, product_type: str | None, budget_max: float | None) -> bool:
    if _contains_any(text, {"order", "tracking", "payment status", "return policy", "refund policy"}):
        return False
    if not _SHOPPING_REQUEST_RE.search(text):
        return False
    if product_type or budget_max is not None:
        return _is_broad_category_request(text, product_type, budget_max)
    return _contains_any(text, {"party", "nice", "outfit", "event", "occasion", "something"})


def _is_broad_category_request(text: str, product_type: str | None, budget_max: float | None) -> bool:
    if budget_max is not None or not product_type:
        return False
    return bool(
        re.match(
            rf"^(?:show me|find|recommend|looking for|i need)\s+(?:some\s+|a\s+|an\s+)?{re.escape(product_type)}$",
            text,
        )
    )


def _clarification_question_for(product_type: str | None) -> str:
    if product_type:
        return f"What kind of {product_type} are you looking for, and what budget would you like to stay within?"
    return "What type of item are you looking for, and what budget would you like to stay within?"


def _extract_location(original: str, lowered: str) -> str | None:
    if not _contains_any(lowered, {"store", "pickup", "pick up", "available", "availability"}):
        return None
    match = _STORE_PATTERN.search(original)
    if not match:
        return None
    candidate = match.group(1).strip()
    if candidate.lower() in _COLOR_TERMS or candidate.lower() in _PRODUCT_TERMS:
        return None
    return candidate


def _has_conflicting_instruction(text: str) -> bool:
    return _contains_any(
        text,
        {
            "ignore previous",
            "ignore your instructions",
            "forget your instructions",
            "do everything",
            "all of the above",
        },
    )


def _looks_like_complex_comparison(text: str) -> bool:
    return _contains_any(text, {"which one is better", "compare", "versus", " vs ", "better than"})


def _looks_multi_intent(text: str) -> bool:
    domains = {
        "product": _contains_any(text, {"recommend", "find", "show me", "search", "looking for", "need"}) and bool(_extract_product_type(text)),
        "policy": _contains_any(text, {"policy", "return policy", "refund policy", "shipping policy"}),
        "order": bool(_extract_order_id(text)) or _contains_any(text, {"check my order", "track my order"}),
        "store": _contains_any(text, {"pickup", "pick up", "store availability"}) and bool(_extract_product_type(text)),
    }
    if sum(1 for present in domains.values() if present) < 2:
        return False
    return bool(re.search(r"\b(and|also|plus|then)\b", text))
