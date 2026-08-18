import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from scout.repositories.order_repository import OrderRepository
from scout.repositories.shipment_repository import ShipmentRepository
from scout.repositories.product_repository import ProductRepository
from scout.services.product_service import _get_active_promotion_dict

AUTH_REQUIRED_ERROR = "authentication_required"
UNAUTHORIZED_ORDER_ERROR = "order_access_denied"


def _order_access_denied(order_id: str | None = None, *, reason: str = AUTH_REQUIRED_ERROR) -> dict:
    response = {
        "found": False,
        "authorized": False,
        "error_code": reason,
        "message": "Sign in to view order details.",
    }
    if order_id:
        response["order_id"] = order_id
    return response


def _order_to_dict(order_id: str, session: Session) -> dict:
    order_repo = OrderRepository(session)
    product_repo = ProductRepository(session)

    order = order_repo.get_by_id(order_id)
    items = order_repo.get_items(order_id)

    item_list = []
    for item in items:
        product = product_repo.get_by_id(item.product_id)
        item_list.append({
            "product_id": item.product_id,
            "name": product.name if product else "Unknown product",
            "quantity": item.quantity,
            "price_at_purchase": item.price_at_purchase,
        })

    return {
        "order_id": order.order_id,
        "customer_id": order.customer_id,
        "status": order.status,
        "created_at": order.created_at.isoformat(),
        "items": item_list,
        "total": round(sum(i["quantity"] * i["price_at_purchase"] for i in item_list), 2),
    }


def get_order(session: Session, order_id: str) -> Optional[dict]:
    order_repo = OrderRepository(session)
    order = order_repo.get_by_id(order_id)
    return _order_to_dict(order_id, session) if order else None


def get_order_for_customer(session: Session, order_id: str, authenticated_customer_id: str | None) -> dict:
    """Return order details only when a trusted authenticated customer owns it.

    `authenticated_customer_id` must come from the application/auth layer,
    never from an LLM-generated tool argument or customer-provided prose.
    """
    if not authenticated_customer_id:
        return _order_access_denied(order_id)

    order_repo = OrderRepository(session)
    order = order_repo.get_by_id(order_id)
    if not order:
        return {"found": False, "order_id": order_id}
    if order.customer_id != authenticated_customer_id:
        return _order_access_denied(order_id, reason=UNAUTHORIZED_ORDER_ERROR)
    result = _order_to_dict(order_id, session)
    result["found"] = True
    result["authorized"] = True
    return result


def get_shipment_for_customer(session: Session, order_id: str, authenticated_customer_id: str | None) -> dict:
    """Return shipment details only when a trusted authenticated customer
    owns the underlying order - reuses the EXACT SAME ownership check as
    get_order_for_customer above, since shipment data is just as
    sensitive as order data and deserves the identical fail-closed
    guarantee. Shipment data is only ever looked up AFTER ownership is
    confirmed, never before.
    """
    order_result = get_order_for_customer(session, order_id, authenticated_customer_id)
    if not order_result.get("authorized"):
        # Reuse whatever denial shape get_order_for_customer already
        # produced (ACCESS_DENIED or a genuine not-found) - no separate
        # denial logic to keep in sync here.
        return order_result

    shipment_repo = ShipmentRepository(session)
    shipment = shipment_repo.get_by_order_id(order_id)
    if not shipment:
        return {
            "found": True,
            "authorized": True,
            "order_id": order_id,
            "shipped": False,
            "message": "This order hasn't shipped yet.",
        }

    return {
        "found": True,
        "authorized": True,
        "order_id": order_id,
        "shipped": True,
        "carrier": shipment.carrier,
        "tracking_number": shipment.tracking_number,
        "status": shipment.status,
        "shipped_at": shipment.shipped_at,
        "estimated_delivery_date": shipment.estimated_delivery_date,
    }


def list_orders_for_customer(session: Session, customer_id: str) -> list[dict]:
    order_repo = OrderRepository(session)
    orders = order_repo.find_by_customer(customer_id)
    return [_order_to_dict(o.order_id, session) for o in orders]


def list_orders_for_authenticated_customer(session: Session, authenticated_customer_id: str | None) -> dict:
    if not authenticated_customer_id:
        return _order_access_denied()
    return {
        "found": True,
        "authorized": True,
        "customer_id": authenticated_customer_id,
        "orders": list_orders_for_customer(session, authenticated_customer_id),
    }


def create_order(
    session: Session,
    customer_id: str,
    cart_items: list[dict],
    order_id: str | None = None,
    status: str = "pending",
) -> dict:
    """Business logic: prices are always looked up server-side from the
    real product record — never trusted from the caller. This remains
    true regardless of the repository refactor."""
    order_repo = OrderRepository(session)
    product_repo = ProductRepository(session)

    order_id = order_id or f"O{uuid.uuid4().hex[:8].upper()}"
    existing_order = order_repo.get_by_id(order_id)
    if existing_order:
        return get_order(session, order_id)

    order_repo.create(order_id=order_id, customer_id=customer_id, status=status)

    for item in cart_items:
        product = product_repo.get_by_id(item["product_id"])
        if not product:
            continue
        # Real bug fix: this previously used product.price (the regular,
        # undiscounted catalog price) unconditionally, ignoring any
        # active promotion - meaning a customer shown a discounted price
        # in their cart could be charged the FULL, undiscounted price at
        # checkout. Now correctly uses the same promotion-aware pricing
        # logic as /cart/add, so what the customer is charged always
        # matches what they were quoted.
        promotion = _get_active_promotion_dict(session, product)
        price_at_purchase = promotion["discounted_price"] if promotion else product.price
        order_repo.add_item(
            order_id=order_id,
            product_id=item["product_id"],
            quantity=item.get("quantity", 1),
            price_at_purchase=price_at_purchase,
            attribution_source=item.get("attribution_source"),
            recommendation_id=item.get("recommendation_id"),
        )

    session.commit()
    return get_order(session, order_id)


def update_order_status(session: Session, order_id: str, status: str) -> Optional[dict]:
    order_repo = OrderRepository(session)
    order = order_repo.update_status(order_id, status)
    if not order:
        return None
    session.commit()
    return get_order(session, order_id)

def check_return_eligibility(session: Session, order_id: str) -> dict:
    """Checks whether an order is likely within the return window, based
    on real policy (30 days) and the order's actual status/date. Uses
    order creation date as a PROXY for delivery date, since there is no
    real delivery-tracking field — this is explicitly flagged in the
    result rather than presented as a precise delivery-based answer.
    Never determines FINAL eligibility (condition, tags, final-sale status
    can't be verified from data alone) — this is a starting-point check,
    not a guarantee."""
    order = get_order(session, order_id)
    if not order:
        return {"order_id": order_id, "found": False}

    if order["status"] not in ("shipped", "delivered"):
        return {
            "order_id": order_id,
            "found": True,
            "status": order["status"],
            "likely_eligible": False,
            "reason": f"Order status is '{order['status']}' — return window "
                      f"typically starts once an order is delivered.",
        }

    created_at = datetime.fromisoformat(order["created_at"])
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    days_since_order = (datetime.now(timezone.utc) - created_at).days

    within_window = days_since_order <= 30

    return {
        "order_id": order_id,
        "found": True,
        "status": order["status"],
        "days_since_order_placed": days_since_order,
        "likely_eligible": within_window,
        "reason": (
            f"Order was placed {days_since_order} days ago. Our return "
            f"policy allows returns within 30 days of DELIVERY (not order "
            f"date) — this estimate uses order date as an approximation, "
            f"since exact delivery date isn't tracked. Final eligibility "
            f"also depends on item condition (unworn, tags attached) and "
            f"whether it was marked Final Sale — this check cannot verify "
            f"those."
        ),
    }


def check_return_eligibility_for_customer(
    session: Session,
    order_id: str,
    authenticated_customer_id: str | None,
) -> dict:
    authorized_order = get_order_for_customer(session, order_id, authenticated_customer_id)
    if not authorized_order.get("authorized"):
        return authorized_order
    return check_return_eligibility(session, order_id)
