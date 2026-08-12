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
    if RECOVERY_URGENCY_RE.search(normalized_message) or RECOVERY_STORE_RE.search(normalized_message):
        return "store_availability"
    if RECOVERY_BEST_FULFILLMENT_RE.search(normalized_message):
        return "store_availability" if (RECOVERY_URGENCY_RE.search(normalized_message) or RECOVERY_STORE_RE.search(normalized_message)) else "best_fulfillment"
    return None
