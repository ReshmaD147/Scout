from typing import Optional

from sqlalchemy.orm import Session

from scout.db.models import Shipment


class ShipmentRepository:
    """Read-only data access for Shipment, from Scout's perspective.

    Shipment records are created/updated exclusively by deterministic
    fulfillment logic (outside this project's current scope) - Scout's
    agents only ever read shipment data, never write it. This mirrors
    the same "agents get read-only access, mutation stays in
    deterministic code" boundary already established for orders,
    payments, and every other commerce-sensitive table.
    """

    def __init__(self, session: Session):
        self.session = session

    def get_by_order_id(self, order_id: str) -> Optional[Shipment]:
        """Return the shipment for a given order, or None if the order
        hasn't shipped yet (no Shipment row exists) or doesn't exist.
        """
        return self.session.query(Shipment).filter_by(order_id=order_id).first()
