from __future__ import annotations

from typing import Any

DIRECT_ROUTE_SPECIALISTS = {
    "product_recommendation": "recommend_agent",
    "similar_products": "recommend_agent",
    "inventory_availability": "inventory_agent",
    "store_availability": "inventory_agent",
    "order_status": "order_agent",
    "return_eligibility": "order_agent",
    "policy_question": "policy_agent",
    "external_offer": "external_offer_agent",
}
DETERMINISTIC_CONVERSATIONAL_REPLIES = {
    "greeting": "Hi — how can I help you shop today?",
    "thanks": "You're welcome — happy to help.",
    "purchase_execution": (
        "I can help you select the product and prepare your cart, but payment must be "
        "completed through Scout's secure storefront checkout."
    ),
    "shopping_clarification": (
        "What type of item are you looking for, and what budget would you like to stay within?"
    ),
    "out_of_scope": (
        "I don’t have live access for that kind of request here, but I can help with Scout products, "
        "availability, orders, checkout guidance, and return or refund policy."
    ),
}


def _get_specialist_registry(app) -> dict:
    registry = getattr(app, "scout_specialists", None)
    return registry if isinstance(registry, dict) else {}


def _direct_route_agent(app, structured_intent: Any | None, sub_intents: list[str]) -> str | None:
    if structured_intent is None or len(sub_intents) != 1:
        return None
    if structured_intent.needs_clarification:
        return None
    specialist_name = DIRECT_ROUTE_SPECIALISTS.get(structured_intent.request_type)
    if specialist_name is None:
        return None
    return specialist_name if specialist_name in _get_specialist_registry(app) else None


def _deterministic_conversational_reply(structured_intent: Any | None, sub_intents: list[str]) -> str | None:
    if structured_intent is None or len(sub_intents) != 1:
        return None
    if structured_intent.request_type == "shopping_clarification" and structured_intent.clarification_question:
        return structured_intent.clarification_question
    return DETERMINISTIC_CONVERSATIONAL_REPLIES.get(structured_intent.request_type)
