from typing import Optional
import hashlib
import json
import uuid

from fastapi import APIRouter
from pydantic import BaseModel

from scout.db.session import SessionLocal
from scout.repositories.product_repository import ProductRepository
from scout.services.order_service import create_order, get_order
from scout.services.payment_service import create_test_payment
from scout.services.payment_service import retrieve_test_payment, PaymentProcessingError
from scout.services.product_service import check_stock, _get_active_promotion_dict

router = APIRouter()


class CheckoutItem(BaseModel):
    product_id: str
    quantity: int = 1
    size: str | None = None
    color: str | None = None
    # Carried through from the earlier /cart/add response, then revalidated
    # here against the recommendation registry and original chat session
    # before any revenue is counted as Scout-attributed.
    attribution_source: str | None = None
    recommendation_id: str | None = None
    recommendation_session_id: str | None = None


class CheckoutRequest(BaseModel):
    items: list[CheckoutItem]
    customer_id: Optional[str] = "guest"
    session_id: str | None = None


class CheckoutFinalizeRequest(CheckoutRequest):
    payment_intent_id: str


def _cart_fingerprint(items: list[CheckoutItem], session_id: str | None = None) -> str:
    canonical_items = sorted(
        (
            {
                "product_id": item.product_id,
                "quantity": item.quantity,
                "size": item.size or "",
                "color": (item.color or "").strip().lower(),
                "attribution_source": item.attribution_source or "",
                "recommendation_id": item.recommendation_id or "",
                "recommendation_session_id": session_id or "",
            }
            for item in items
        ),
        key=lambda item: (
            item["product_id"], item["size"], item["color"], item["quantity"]
        ),
    )
    payload = json.dumps(canonical_items, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def _validated_cart_items(
    session,
    items: list[CheckoutItem],
    recommendation_session_id: str | None = None,
):
    product_repo = ProductRepository(session)
    cart_items = []
    for item in items:
        if item.quantity < 1:
            return None, "Quantity must be at least 1"
        product = product_repo.get_by_id(item.product_id)
        if not product:
            return None, "Could not create order — check product IDs."

        stock_info = check_stock(session, item.product_id, size=item.size, color=item.color)
        if not stock_info["in_stock"] or item.quantity > stock_info["total_quantity"]:
            variant = []
            if stock_info["requested_color"]:
                variant.append(stock_info["requested_color"].title())
            if stock_info["requested_size"]:
                variant.append(f"size {stock_info['requested_size']}")
            product_label = f"{product.name} in {', '.join(variant)}" if variant else product.name
            if not stock_info["in_stock"]:
                return None, f"{product_label} is currently out of stock."
            return None, f"Only {stock_info['total_quantity']} available for {product_label}."

        from scout.agents.attribution import validate_recommendation
        is_genuinely_attributed = bool(
            item.recommendation_id
            and recommendation_session_id
            and validate_recommendation(
                item.recommendation_id,
                item.product_id,
                recommendation_session_id,
            )
        )
        promotion = _get_active_promotion_dict(session, product)
        price = promotion["discounted_price"] if promotion else product.price
        cart_items.append({
            "product_id": item.product_id,
            "quantity": item.quantity,
            "attribution_source": "scout" if is_genuinely_attributed else None,
            "recommendation_id": item.recommendation_id if is_genuinely_attributed else None,
            "_computed_price": price,
        })
    return cart_items, None


@router.post("/checkout")
def checkout(request: CheckoutRequest):
    session = SessionLocal()
    try:
        cart_items, error = _validated_cart_items(
            session,
            request.items,
            recommendation_session_id=request.session_id,
        )
        if error:
            return {"success": False, "error": error}
        precomputed_total = sum(
            ci["_computed_price"] * ci["quantity"] for ci in cart_items
        )
        order_id = f"O{uuid.uuid4().hex[:8].upper()}"

        try:
            payment = create_test_payment(
                amount_usd=precomputed_total,
                description=f"Scout order {order_id}",
                metadata={
                    "order_id": order_id,
                    "cart_fingerprint": _cart_fingerprint(
                        request.items,
                        session_id=request.session_id,
                    ),
                },
            )
        except PaymentProcessingError as e:
            return {"success": False, "error": str(e)}

        return {"success": True, "total": round(precomputed_total, 2), "payment": payment}
    finally:
        session.close()


@router.post("/checkout/finalize")
def finalize_checkout(request: CheckoutFinalizeRequest):
    session = SessionLocal()
    try:
        try:
            payment = retrieve_test_payment(request.payment_intent_id)
        except PaymentProcessingError as e:
            return {"success": False, "error": str(e)}

        if payment["status"] != "succeeded":
            return {"success": False, "error": "Payment has not completed successfully."}
        if payment["currency"] != "usd":
            return {"success": False, "error": "Payment currency does not match checkout."}

        order_id = payment["metadata"].get("order_id")
        if not order_id:
            return {"success": False, "error": "Payment is missing its order reference."}
        if payment["metadata"].get("cart_fingerprint") != _cart_fingerprint(
            request.items,
            session_id=request.session_id,
        ):
            return {"success": False, "error": "Checkout items do not match the completed payment."}

        existing_order = get_order(session, order_id)
        if existing_order:
            return {"success": True, "order": existing_order, "payment": payment}

        cart_items, error = _validated_cart_items(
            session,
            request.items,
            recommendation_session_id=request.session_id,
        )
        if error:
            return {"success": False, "error": error}
        expected_amount = int(round(sum(
            item["_computed_price"] * item["quantity"] for item in cart_items
        ) * 100))
        if payment["amount"] != expected_amount or payment["amount_received"] != expected_amount:
            return {"success": False, "error": "Payment amount does not match checkout."}

        for item in cart_items:
            item.pop("_computed_price", None)
        order = create_order(
            session,
            customer_id=request.customer_id,
            cart_items=cart_items,
            order_id=order_id,
            status="processing",
        )
        return {"success": True, "order": order, "payment": payment}
    finally:
        session.close()
