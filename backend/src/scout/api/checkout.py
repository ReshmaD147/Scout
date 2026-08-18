from typing import Optional
import hashlib
import json
import re
import uuid

from fastapi import APIRouter
from pydantic import BaseModel, Field, field_validator

from scout.db.session import SessionLocal
from scout.repositories.product_repository import ProductRepository
from scout.services.order_service import create_order, get_order
from scout.services.payment_service import create_test_payment
from scout.services.payment_service import retrieve_test_payment, PaymentProcessingError
from scout.services.product_service import check_stock, _get_active_promotion_dict
from scout.services.store_service import SHIPPING_FREE_THRESHOLD, SHIPPING_STANDARD_FEE

router = APIRouter()

MINNESOTA_MERCHANDISE_TAX_RATE = 0.06875


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


class CheckoutAddress(BaseModel):
    full_name: str = Field(min_length=1, max_length=120)
    address_line1: str = Field(min_length=1, max_length=160)
    address_line2: str | None = Field(default=None, max_length=160)
    city: str = Field(min_length=1, max_length=100)
    state: str = Field(min_length=1, max_length=80)
    postal_code: str = Field(min_length=1, max_length=20)
    country: str = Field(default="US", min_length=2, max_length=60)

    @field_validator("*", mode="before")
    @classmethod
    def trim_strings(cls, value):
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value


class CheckoutRequest(BaseModel):
    items: list[CheckoutItem]
    customer_id: Optional[str] = "guest"
    session_id: str | None = None
    contact_email: str | None = None
    shipping_address: CheckoutAddress | None = None
    billing_same_as_shipping: bool = True
    billing_address: CheckoutAddress | None = None

    @field_validator("contact_email")
    @classmethod
    def contact_email_valid(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lower()
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", normalized):
            raise ValueError("contact_email must be a valid email address")
        return normalized

    @field_validator("billing_address")
    @classmethod
    def billing_address_required_when_different(cls, value, info):
        if info.data.get("billing_same_as_shipping") is False and value is None:
            raise ValueError("billing_address is required when billing differs from shipping")
        return value


class CheckoutFinalizeRequest(CheckoutRequest):
    payment_intent_id: str


def _address_fingerprint(address: CheckoutAddress | None) -> dict:
    if address is None:
        return {}
    return address.model_dump()


def _checkout_details_fingerprint(request: CheckoutRequest | None = None) -> dict:
    if request is None:
        return {}
    return {
        "contact_email": str(request.contact_email or "").strip().lower(),
        "shipping_address": _address_fingerprint(request.shipping_address),
        "billing_same_as_shipping": bool(request.billing_same_as_shipping),
        "billing_address": _address_fingerprint(
            request.shipping_address if request.billing_same_as_shipping else request.billing_address
        ),
    }


def _cart_fingerprint(
    items: list[CheckoutItem],
    session_id: str | None = None,
    checkout_details: CheckoutRequest | None = None,
) -> str:
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
    payload = json.dumps(
        {
            "items": canonical_items,
            "checkout_details": _checkout_details_fingerprint(checkout_details),
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _checkout_details_to_order_fields(request: CheckoutRequest, payment: dict | None = None) -> dict:
    shipping = request.shipping_address
    billing = request.shipping_address if request.billing_same_as_shipping else request.billing_address
    details = {
        "contact_email": str(request.contact_email) if request.contact_email else None,
        "billing_same_as_shipping": request.billing_same_as_shipping,
        "stripe_receipt_email": (payment or {}).get("receipt_email") or (str(request.contact_email) if request.contact_email else None),
        "stripe_receipt_status": "sent_by_stripe_test_mode" if request.contact_email else None,
    }
    if shipping:
        details.update({
            "shipping_name": shipping.full_name,
            "shipping_address_line1": shipping.address_line1,
            "shipping_address_line2": shipping.address_line2,
            "shipping_city": shipping.city,
            "shipping_state": shipping.state,
            "shipping_postal_code": shipping.postal_code,
            "shipping_country": shipping.country,
        })
    if billing:
        details.update({
            "billing_name": billing.full_name,
            "billing_address_line1": billing.address_line1,
            "billing_address_line2": billing.address_line2,
            "billing_city": billing.city,
            "billing_state": billing.state,
            "billing_postal_code": billing.postal_code,
            "billing_country": billing.country,
        })
    return details


def _checkout_totals(subtotal: float) -> dict:
    rounded_subtotal = round(float(subtotal), 2)
    shipping = 0.0 if rounded_subtotal >= SHIPPING_FREE_THRESHOLD else SHIPPING_STANDARD_FEE
    tax = round(rounded_subtotal * MINNESOTA_MERCHANDISE_TAX_RATE, 2)
    total = round(rounded_subtotal + shipping + tax, 2)
    return {
        "subtotal": rounded_subtotal,
        "shipping": round(shipping, 2),
        "tax": tax,
        "total": total,
        "tax_rate": MINNESOTA_MERCHANDISE_TAX_RATE,
    }


def _checkout_details_error(request: CheckoutRequest) -> str | None:
    if not request.contact_email:
        return "Contact email is required."
    if not request.shipping_address:
        return "Shipping address is required."
    if not request.billing_same_as_shipping and not request.billing_address:
        return "Billing address is required."
    return None


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
        details_error = _checkout_details_error(request)
        if details_error:
            return {"success": False, "error": details_error}
        precomputed_subtotal = sum(
            ci["_computed_price"] * ci["quantity"] for ci in cart_items
        )
        totals = _checkout_totals(precomputed_subtotal)
        order_id = f"O{uuid.uuid4().hex[:8].upper()}"

        try:
            payment = create_test_payment(
                amount_usd=totals["total"],
                description=f"Scout order {order_id}",
                metadata={
                    "order_id": order_id,
                    "cart_fingerprint": _cart_fingerprint(
                        request.items,
                        session_id=request.session_id,
                        checkout_details=request,
                    ),
                },
                receipt_email=str(request.contact_email) if request.contact_email else None,
            )
        except PaymentProcessingError as e:
            return {"success": False, "error": str(e)}

        return {"success": True, **totals, "payment": payment}
    finally:
        session.close()


@router.post("/checkout/finalize")
def finalize_checkout(request: CheckoutFinalizeRequest):
    session = SessionLocal()
    try:
        details_error = _checkout_details_error(request)
        if details_error:
            return {"success": False, "error": details_error}
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
            checkout_details=request,
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
        expected_subtotal = sum(
            item["_computed_price"] * item["quantity"] for item in cart_items
        )
        totals = _checkout_totals(expected_subtotal)
        expected_amount = int(round(totals["total"] * 100))
        if payment["amount"] != expected_amount or payment["amount_received"] != expected_amount:
            return {"success": False, "error": "Payment amount does not match checkout."}

        for item in cart_items:
            item.pop("_computed_price", None)
        checkout_details = _checkout_details_to_order_fields(request, payment)
        order = create_order(
            session,
            customer_id=request.customer_id,
            cart_items=cart_items,
            order_id=order_id,
            status="processing",
            checkout_details=checkout_details,
        )
        order.update({
            "subtotal": totals["subtotal"],
            "shipping_total": totals["shipping"],
            "tax_total": totals["tax"],
            "total": totals["total"],
        })
        return {"success": True, "order": order, "payment": payment}
    finally:
        session.close()
