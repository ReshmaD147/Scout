from typing import Optional

from sqlalchemy.orm import Session

from scout.db.models import Store, StoreStock


class StoreRepository:
    """Read-only data access for Store and StoreStock (inventory at
    specific physical store locations, separate from warehouse Stock).
    """

    def __init__(self, session: Session):
        self.session = session

    def get_by_id(self, store_id: str) -> Optional[Store]:
        """Return a single store by its ID, or None if it doesn't exist."""
        return self.session.query(Store).filter_by(store_id=store_id).first()

    def find_by_name(self, name: str) -> Optional[Store]:
        """Return the first store whose name partially matches (case-insensitive)."""
        return self.session.query(Store).filter(Store.name.ilike(f"%{name}%")).first()

    def get_all(self) -> list[Store]:
        """Return every store."""
        return self.session.query(Store).all()

    def get_stock_for_product(
        self, product_id: str, store_name: Optional[str] = None
    ) -> list[tuple[StoreStock, Store]]:
        """Return store-level stock for a product, paired with each
        store's details. Returns (StoreStock, Store) tuples rather than
        just StoreStock rows because callers need the store's name to
        build a useful reply (e.g. "available at Downtown Minneapolis"),
        not just an internal store_id.

        If store_name is given, narrows to stores whose name partially
        matches — used for "is it in stock at Maple Grove" style queries.
        """
        q = (
            self.session.query(StoreStock, Store)
            .join(Store, StoreStock.store_id == Store.store_id)
            .filter(StoreStock.product_id == product_id)
        )
        if store_name:
            q = q.filter(Store.name.ilike(f"%{store_name}%"))
        return q.all()