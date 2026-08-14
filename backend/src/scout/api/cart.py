from fastapi import APIRouter
from pydantic import BaseModel

from scout.db.session import SessionLocal
from scout.services.product_service import add_to_cart_service

router = APIRouter()


class AddToCartRequest(BaseModel):
    product_id: str
    quantity: int = 1
    size: str | None = None
    color: str | None = None
    # Optional: present only when this cart-add originated from a Scout
    # recommendation card. Validated below - never trusted at face value,
    # since a customer's browser could otherwise claim any arbitrary
    # cart-add came from Scout. An invalid/missing ID does NOT block the
    # cart-add itself (the customer should never be prevented from buying
    # something over an attribution technicality) - it just means this
    # item won't be counted as Scout-assisted revenue.
    recommendation_id: str | None = None


@router.post("/cart/add")
def add_to_cart(request: AddToCartRequest):
    """Authoritative add-to-cart validation endpoint. The frontend never
    sends a price — only a product_id and quantity. Delegates to
    add_to_cart_service (product_service.py), the single shared source of
    truth also used by Phase 2's proactive cart-offer confirmation - so
    there is exactly one place real stock/price validation and
    attribution checking happens, not two copies that could drift.
    """
    session = SessionLocal()
    try:
        return add_to_cart_service(
            session,
            product_id=request.product_id,
            quantity=request.quantity,
            size=request.size,
            color=request.color,
            recommendation_id=request.recommendation_id,
        )
    finally:
        session.close()
