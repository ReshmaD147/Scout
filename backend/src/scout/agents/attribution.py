"""Sales attribution: tracks which specific product recommendations Scout
made, so a later cart-add can be validated as genuinely originating from
a real Scout recommendation - not just trusted from the frontend's claim.

In-memory only, matching the existing SESSION_HISTORIES pattern in
api/chat.py - genuinely fine for this project's scale, same tradeoff
already made for chat history.
"""

import uuid

# Maps recommendation_id -> {"product_id": str, "session_id": str}
# A recommendation_id is only ever valid for the exact product it was
# generated for - this is what prevents a customer (or a bug) from
# claiming an arbitrary product's cart-add came from Scout.
_RECOMMENDATION_REGISTRY: dict[str, dict] = {}


def register_recommendation(product_id: str, session_id: str) -> str:
    """Generate a new recommendation_id for this specific product, tied to
    this specific chat session. Called once per product, each time Scout
    renders it as part of an approved, verified recommendation.
    """
    recommendation_id = f"rec_{uuid.uuid4().hex[:12]}"
    _RECOMMENDATION_REGISTRY[recommendation_id] = {
        "product_id": product_id,
        "session_id": session_id,
    }
    return recommendation_id


def validate_recommendation(recommendation_id: str, product_id: str) -> bool:
    """Confirm a recommendation_id is real and genuinely matches the
    product being added to cart. Returns False for any mismatch, an
    unknown ID, or a missing ID - fail closed, same principle as the
    existing order-authorization check.
    """
    if not recommendation_id:
        return False
    entry = _RECOMMENDATION_REGISTRY.get(recommendation_id)
    if not entry:
        return False
    return entry["product_id"] == product_id
