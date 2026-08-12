from typing import Optional

from sqlalchemy.orm import Session

from scout.db.models import Order, OrderItem


class OrderRepository:
    """Data access for Order and OrderItem.

    Unlike the read-only repositories, this one also creates and updates
    rows — but it never commits the session itself. Committing is left
    to the calling service, so an order and its items can be created
    together and saved atomically in one transaction.
    """

    def __init__(self, session: Session):
        self.session = session

    def get_by_id(self, order_id: str) -> Optional[Order]:
        """Return a single order by its ID, or None if it doesn't exist."""
        return self.session.query(Order).filter_by(order_id=order_id).first()

    def get_items(self, order_id: str) -> list[OrderItem]:
        """Return every line item belonging to an order."""
        return self.session.query(OrderItem).filter_by(order_id=order_id).all()

    def find_by_customer(self, customer_id: str) -> list[Order]:
        """Return every order placed by a given customer."""
        return self.session.query(Order).filter_by(customer_id=customer_id).all()

    def create(self, order_id: str, customer_id: str, status: str = "pending") -> Order:
        """Build a new Order and stage it for saving. Does not commit —
        the caller commits once, after also adding line items.
        """
        order = Order(order_id=order_id, customer_id=customer_id, status=status)
        self.session.add(order)
        return order

    def add_item(
        self, order_id: str, product_id: str, quantity: int, price_at_purchase: float
    ) -> OrderItem:
        """Build a new OrderItem and stage it for saving. Does not commit.

        price_at_purchase is stored separately from the product's current
        price, so historical orders stay accurate even if the product's
        price changes later.
        """
        item = OrderItem(
            order_id=order_id,
            product_id=product_id,
            quantity=quantity,
            price_at_purchase=price_at_purchase,
        )
        self.session.add(item)
        return item

    def update_status(self, order_id: str, status: str) -> Optional[Order]:
        """Update an order's status. Returns the updated Order, or None
        if no order with that ID exists — the caller is responsible for
        checking this and handling a "not found" case appropriately.
        """
        order = self.get_by_id(order_id)
        if order:
            order.status = status
        return order