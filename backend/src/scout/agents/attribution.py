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


def validate_recommendation(
    recommendation_id: str,
    product_id: str,
    session_id: str | None = None,
) -> bool:
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
    if entry["product_id"] != product_id:
        return False
    if session_id is not None and entry["session_id"] != session_id:
        return False
    return True


def apply_attribution_and_cart_offer(
    *, products: list[dict], reply: str, session_id: str, conversation_context: dict
) -> str:
    """Shared logic for tagging recommended products with a real
    recommendation_id and, for a genuinely unambiguous single product,
    proactively offering to add it to cart. Extracted so both the
    non-streaming /chat endpoint and the streaming ask_streaming() path
    apply the EXACT same attribution/cart-offer behavior - previously,
    this logic only existed in api/chat.py, meaning recommendations
    delivered via streaming (which is what the actual chat widget UI
    uses exclusively) never received a recommendation_id at all, and the
    automatic single-product cart offer never fired for streamed
    responses. Confirmed via code review as a real, significant gap.

    Mutates `products` in place (adding recommendation_id) and
    `conversation_context` in place (setting pending_cart_offer), and
    returns the possibly-appended reply text.
    """
    for product in products:
        internal_product_id = product.get("product_id")
        if internal_product_id:
            product["recommendation_id"] = register_recommendation(
                product_id=internal_product_id,
                session_id=session_id,
            )
            product["recommendation_session_id"] = session_id

    internal_products = [p for p in products if p.get("source") == "internal"]
    if len(internal_products) == 1 and not conversation_context.get("pending_cart_offer"):
        offer_product = internal_products[0]
        conversation_context["pending_cart_offer"] = {
            "product_id": offer_product.get("product_id"),
            "product_name": offer_product.get("name"),
            "size": offer_product.get("size"),
            "color": offer_product.get("color"),
            "quantity": 1,
            "recommendation_id": offer_product.get("recommendation_id"),
            "recommendation_session_id": offer_product.get("recommendation_session_id"),
        }
        reply = f"{reply} Want me to add the {offer_product.get('name')} to your cart?"

    return reply
