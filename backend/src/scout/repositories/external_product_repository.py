from typing import Optional

from sqlalchemy.orm import Session

from scout.db.models import ExternalProduct


class ExternalProductRepository:
    """Read-only data access for ExternalProduct — the small, curated
    catalog of real third-party vendor products used as a fallback when
    the internal catalog has no genuine match for a request.
    """

    def __init__(self, session: Session):
        self.session = session

    def get_by_id(self, external_product_id: str) -> Optional[ExternalProduct]:
        """Return a single external product by its ID, or None if it
        doesn't exist.
        """
        return (
            self.session.query(ExternalProduct)
            .filter_by(external_product_id=external_product_id)
            .first()
        )

    def find_by_category(self, category: str) -> list[ExternalProduct]:
        """Return external products whose category partially matches
        (case-insensitive). Pass an empty string to get every product.
        """
        q = self.session.query(ExternalProduct)
        if category:
            q = q.filter(ExternalProduct.category.ilike(f"%{category}%"))
        return q.all()

    def count_by_category(self, category: str) -> int:
        """Count external products whose category partially matches.
        Pass an empty string to count every product.
        """
        q = self.session.query(ExternalProduct)
        if category:
            q = q.filter(ExternalProduct.category.ilike(f"%{category}%"))
        return q.count()