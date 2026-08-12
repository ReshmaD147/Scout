from typing import Optional

from sqlalchemy.orm import Session

from scout.db.models import Promotion


class PromotionRepository:
    """Read-only data access for Promotion.

    Both lookups filter to active=True, so callers never see expired
    promotions. Assumes at most one active promotion per product or
    category at a time — if more than one exists, only one is returned,
    with no defined tie-breaking rule.
    """

    def __init__(self, session: Session):
        self.session = session

    def get_for_product(self, product_id: str) -> Optional[Promotion]:
        """Return the active promotion for a specific product, if any."""
        return (
            self.session.query(Promotion)
            .filter_by(product_id=product_id, active=True)
            .first()
        )

    def get_for_category(self, category: str) -> Optional[Promotion]:
        """Return the active promotion for a whole category, if any."""
        return (
            self.session.query(Promotion)
            .filter_by(category=category, active=True)
            .first()
        )