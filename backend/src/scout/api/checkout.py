from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

from scout.db.session import SessionLocal
from scout.repositories.product_repository import ProductRepository
from scout.services.order_service import create_order
from scout.services.payment_service import create_test_payment, PaymentProcessingError
from scout.services.product_service import check_stock, _get_active_promotion_dict

router = APIRouter()


class CheckoutItem(BaseModel):
    product_id: str
    quantity: int = 1
    size: str | None = None
    color: str | None = None
    # Carried through from the earlier /cart/add response, where these
    # were already independently validated - checkout trusts them as-is
    # here, since re-validating a recommendation_id against a conversation
    # that may have ended isn't meaningful; the real validation already
    # happened once, at the moment of the actual cart-add.
    attribution_source: str | None = None
    recommendation_id: str | None = None


class CheckoutRequest(BaseModel):
    items: list[CheckoutItem]
    customer_id: Optional[str] = "guest"


@router.post("/checkout")
def checkout(request: CheckoutRequest):
    session = SessionLocal()
    try:
        product_repo = ProductRepository(session)
        cart_items = []
        for item in request.items:
            if item.quantity < 1:
                return {"success": False, "error": "Quantity must be at least 1"}
            product = product_repo.get_by_id(item.product_id)
            if not product:
                return {"success": False, "error": "Could not create order — check product IDs."}

            stock_info = check_stock(session, item.product_id, size=item.size, color=item.color)
            if not stock_info["in_stock"] or item.quantity > stock_info["total_quantity"]:
                variant = []
                if stock_info["requested_color"]:
                    variant.append(stock_info["requested_color"].title())
                if stock_info["requested_size"]:
                    variant.append(f"size {stock_info['requested_size']}")
                product_label = f"{product.name} in {', '.join(variant)}" if variant else product.name
                if not stock_info["in_stock"]:
                    return {"success": False, "error": f"{product_label} is currently out of stock."}
                return {
                    "success": False,
                    "error": f"Only {stock_info['total_quantity']} available for {product_label}.",
                }

            # Real security fix: never trust attribution_source/
            # recommendation_id directly from the request body - that
            # would let anyone fabricate "scout-attributed" revenue by
            # simply including those fields in a checkout call.
            # Independently re-validate against the real recommendation
            # registry, exactly like /cart/add already does, so only a
            # genuine, previously-issued recommendation_id for THIS
            # product can ever be recorded as Scout-attributed.
            from scout.agents.attribution import validate_recommendation
            is_genuinely_attributed = (
                item.recommendation_id
                and validate_recommendation(item.recommendation_id, item.product_id)
            )
            promotion_for_item = _get_active_promotion_dict(session, product)
            price_for_item = promotion_for_item["discounted_price"] if promotion_for_item else product.price
            cart_items.append({
                "product_id": item.product_id,
                "quantity": item.quantity,
                "attribution_source": "scout" if is_genuinely_attributed else None,
                "recommendation_id": item.recommendation_id if is_genuinely_attributed else None,
                "_computed_price": price_for_item,
            })

        # Real bug fix: previously, create_order() committed the order to
        # the database BEFORE payment was even attempted. If Stripe
        # failed for any reason, a "pending" order was permanently left
        # behind with no successful payment ever having occurred - a
        # genuine orphaned-order problem. Now, the total is computed
        # independently (using the same validated stock/pricing data
        # already gathered above) and payment is attempted FIRST. The
        # order is only created after payment succeeds, so a payment
        # failure never leaves a phantom order in the database.
        precomputed_total = sum(
            ci["_computed_price"] * ci["quantity"] for ci in cart_items
        )

        try:
            payment = create_test_payment(
                amount_usd=precomputed_total,
                description="Scout order",
            )
        except PaymentProcessingError as e:
            return {"success": False, "error": str(e)}

        for ci in cart_items:
            ci.pop("_computed_price", None)

        order = create_order(session, customer_id=request.customer_id, cart_items=cart_items)

        if not order or not order.get("items"):
            return {"success": False, "error": "Could not create order — check product IDs."}

        return {"success": True, "order": order, "payment": payment}
    finally:
        session.close()
