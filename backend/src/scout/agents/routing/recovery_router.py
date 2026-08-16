from __future__ import annotations

import re

RECOVERY_ALTERNATIVE_RE = re.compile(
    r"\b(?:similar|alternatives?|something else|anything else|other options?|another option|like (?:this|it))\b",
    re.IGNORECASE,
)
RECOVERY_DELIVERY_RE = re.compile(r"\b(?:deliver(?:ed|y)?|ship(?:ped|ping)?|send|mail)\b", re.IGNORECASE)
RECOVERY_TRAVEL_AVOIDANCE_RE = re.compile(
    r"\b(?:don'?t|do not|can'?t|cannot|rather not|would rather not|prefer not to|avoid)\b.{0,40}\b(?:drive|go|visit|travel|store|location)\b",
    re.IGNORECASE,
)
RECOVERY_URGENCY_RE = re.compile(
    r"\b(?:today|tonight|this evening|before (?:tonight|this evening|closing)|asap|same[-\s]?day|right away)\b",
    re.IGNORECASE,
)
RECOVERY_STORE_RE = re.compile(r"\b(?:pickup|pick up|nearby|store|stores|location|locations)\b", re.IGNORECASE)
RECOVERY_BEST_FULFILLMENT_RE = re.compile(
    r"\b(?:any way|how can i|can i still|is there a way|is there any way).{0,40}\b(?:get|receive|have)\b",
    re.IGNORECASE,
)
# Signals this is a genuinely NEW, unrelated topic/question, not a
# direct follow-up about the product just discussed - if present,
# urgency words like "today" should never be treated as a recovery
# signal, regardless of message length. Confirmed via a real bug:
# "What is the weather today?" (short, contains "today") was still
# being misclassified without this check.
UNRELATED_TOPIC_RE = re.compile(
    r"^(?:what|who|when|why|how)\b|\bweather\b|\bjoke\b|\bnews\b",
    re.IGNORECASE,
)


def _is_scout_similar_products_request(normalized_message: str) -> bool:
    if any(term in normalized_message for term in ("third-party", "third party", "external", "outside")):
        return False
    return bool(RECOVERY_ALTERNATIVE_RE.search(normalized_message))


def _out_of_stock_recovery_action(normalized_message: str) -> str | None:
    if _is_scout_similar_products_request(normalized_message):
        return "similar_products"
    if RECOVERY_TRAVEL_AVOIDANCE_RE.search(normalized_message):
        return "delivery"
    if RECOVERY_DELIVERY_RE.search(normalized_message) or "online" in normalized_message:
        return "delivery"
    # Urgency words alone (today/tonight/asap) are genuinely ambiguous -
    # e.g. "What is the weather today?" would otherwise falsely match.
    # A real urgency follow-up ("I need it today") is typically SHORT
    # and doesn't introduce an unrelated topic - gate on message length
    # as a simple, honest proxy for "this is a direct follow-up," not a
    # fully-formed new question. Confirmed via a real bug: unrestricted
    # urgency matching caused Scout to get stuck asking for a ZIP code
    # in response to a completely unrelated question.
    is_unrelated_topic = bool(UNRELATED_TOPIC_RE.search(normalized_message))
    is_short_message = len(normalized_message.split()) <= 6
    urgency_signal = (not is_unrelated_topic) and is_short_message and RECOVERY_URGENCY_RE.search(normalized_message)
    if RECOVERY_STORE_RE.search(normalized_message) or urgency_signal:
        return "store_availability"
    if RECOVERY_BEST_FULFILLMENT_RE.search(normalized_message):
        return "store_availability" if (RECOVERY_STORE_RE.search(normalized_message) or urgency_signal) else "best_fulfillment"
    return None
