from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

from scout.db.session import SessionLocal
from scout.repositories.product_repository import ProductRepository
from scout.services.order_service import create_order
from scout.services.payment_service import create_test_payment, PaymentProcessingError
from scout.services.product_service import check_stock

router = APIRouter()


class CheckoutItem(BaseModel):
    product_id: str
    quantity: int = 1
    size: str | None = None
    color: str | None = None


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

            cart_items.append({"product_id": item.product_id, "quantity": item.quantity})
        order = create_order(session, customer_id=request.customer_id, cart_items=cart_items)

        if not order or not order.get("items"):
            return {"success": False, "error": "Could not create order — check product IDs."}

        try:
            payment = create_test_payment(
                amount_usd=order["total"],
                description=f"Order {order['order_id']}",
            )
        except PaymentProcessingError as e:
            return {"success": False, "error": str(e)}

        return {"success": True, "order": order, "payment": payment}
    finally:
        session.close()
