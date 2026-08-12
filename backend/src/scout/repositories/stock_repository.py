from typing import Optional

from sqlalchemy.orm import Session

from scout.db.models import Stock


class StockRepository:
    """Read-only data access for the Stock table (warehouse inventory,
    per size/color variant — separate from StoreStock, which tracks
    inventory at specific physical stores).
    """

    def __init__(self, session: Session):
        self.session = session

    def get_variants(
        self,
        product_id: str,
        size: Optional[str] = None,
        color: Optional[str] = None,
    ) -> list[Stock]:
        """Return stock rows for a product, optionally narrowed to a
        specific size and/or color combination.
        """
        q = self.session.query(Stock).filter_by(product_id=product_id)
        if size:
            q = q.filter(Stock.size.ilike(size))
        if color:
            q = q.filter(Stock.color.ilike(color))
        return q.all()

    def get_in_stock_product_ids(self) -> list[str]:
        """Return the IDs of every product with at least one variant in
        stock. Used to filter recommendation candidates down to items
        that are actually purchasable right now.
        """
        rows = (
            self.session.query(Stock.product_id)
            .filter(Stock.quantity > 0)
            .distinct()
            .all()
        )
        return [r[0] for r in rows]