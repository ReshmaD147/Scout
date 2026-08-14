from fastapi import APIRouter
from sqlalchemy import func

from scout.db.session import SessionLocal
from scout.db.models import OrderItem

router = APIRouter()


@router.get("/analytics/scout-attributed-revenue")
def scout_attributed_revenue():
    """Deterministic, backend-calculated business metric - no AI involved
    in this calculation at all. Sums the REAL, completed order-item value
    (price_at_purchase, not the originally-recommended price) for every
    item genuinely validated as Scout-attributed at cart-add time.

    Example: if Scout recommended an item at $79.99 but the customer
    ultimately paid $67.99 (a promotion applied), this correctly counts
    $67.99, since that's what was actually completed and paid.
    """
    session = SessionLocal()
    try:
        scout_items = (
            session.query(OrderItem)
            .filter(OrderItem.attribution_source == "scout")
            .all()
        )

        total_revenue = sum(item.price_at_purchase * item.quantity for item in scout_items)
        total_items = sum(item.quantity for item in scout_items)
        distinct_orders = len({item.order_id for item in scout_items})

        return {
            "scout_assisted_revenue": round(total_revenue, 2),
            "scout_assisted_orders": distinct_orders,
            "scout_attributed_items": total_items,
        }
    finally:
        session.close()
