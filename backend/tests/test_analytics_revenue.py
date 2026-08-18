from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from scout.api import analytics
from scout.db.models import Base, Order, OrderItem


def test_scout_revenue_excludes_unpaid_pending_orders(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add_all([
        Order(order_id="OPAID", customer_id="C001", status="processing"),
        Order(order_id="OPENDING", customer_id="C001", status="pending"),
        OrderItem(
            order_id="OPAID",
            product_id="P001",
            quantity=2,
            price_at_purchase=20.0,
            attribution_source="scout",
        ),
        OrderItem(
            order_id="OPENDING",
            product_id="P002",
            quantity=3,
            price_at_purchase=50.0,
            attribution_source="scout",
        ),
    ])
    session.commit()
    monkeypatch.setattr(analytics, "SessionLocal", lambda: session)

    result = analytics.scout_attributed_revenue()

    assert result == {
        "scout_assisted_revenue": 40.0,
        "scout_assisted_orders": 1,
        "scout_attributed_items": 2,
    }
