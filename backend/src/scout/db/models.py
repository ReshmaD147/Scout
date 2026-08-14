import uuid
from datetime import datetime, timezone

from sqlalchemy import ForeignKey, String, Float, Integer, DateTime, Boolean, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Product(Base):
    __tablename__ = "products"

    product_id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    brand: Mapped[str] = mapped_column(String, nullable=False, default="")
    department: Mapped[str] = mapped_column(String, nullable=False, default="")
    category: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(String, default="")
    price: Mapped[float] = mapped_column(Float, nullable=False)
    rating: Mapped[float] = mapped_column(Float, default=0.0)
    image_url: Mapped[str] = mapped_column(String, default="")
    tags: Mapped[str] = mapped_column(String, default="")

    stock: Mapped[list["Stock"]] = relationship(back_populates="product")


class Stock(Base):
    __tablename__ = "stock"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.product_id"))
    size: Mapped[str] = mapped_column(String, nullable=True)
    color: Mapped[str] = mapped_column(String, nullable=True)
    quantity: Mapped[int] = mapped_column(Integer, default=0)

    product: Mapped["Product"] = relationship(back_populates="stock")


class Store(Base):
    __tablename__ = "stores"

    store_id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    address: Mapped[str] = mapped_column(String, nullable=False)

    store_stock: Mapped[list["StoreStock"]] = relationship(back_populates="store")


class StoreStock(Base):
    __tablename__ = "store_stock"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    store_id: Mapped[str] = mapped_column(ForeignKey("stores.store_id"))
    product_id: Mapped[str] = mapped_column(ForeignKey("products.product_id"))
    quantity: Mapped[int] = mapped_column(Integer, default=0)

    store: Mapped["Store"] = relationship(back_populates="store_stock")


class Order(Base):
    __tablename__ = "orders"

    order_id: Mapped[str] = mapped_column(String, primary_key=True)
    customer_id: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, default="pending")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )

    items: Mapped[list["OrderItem"]] = relationship(back_populates="order")


class OrderItem(Base):
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.order_id"))
    product_id: Mapped[str] = mapped_column(ForeignKey("products.product_id"))
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    price_at_purchase: Mapped[float] = mapped_column(Float, nullable=False)
    # Sales attribution: set only when this item's cart-add originated from
    # a specific Scout recommendation, validated against a real
    # recommendation_id at checkout time (see checkout.py) - never trusted
    # from the frontend alone, so a customer can't just claim an arbitrary
    # purchase came from Scout.
    attribution_source: Mapped[str | None] = mapped_column(String, nullable=True)
    recommendation_id: Mapped[str | None] = mapped_column(String, nullable=True)

    order: Mapped["Order"] = relationship(back_populates="items")


class Shipment(Base):
    __tablename__ = "shipments"

    shipment_id: Mapped[str] = mapped_column(String, primary_key=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.order_id"))
    carrier: Mapped[str] = mapped_column(String, nullable=False)
    tracking_number: Mapped[str] = mapped_column(String, nullable=False)
    # Deliberately a plain string, not an enum - matches the existing
    # Order.status pattern in this codebase, rather than introducing a
    # new, inconsistent convention for representing state.
    status: Mapped[str] = mapped_column(String, default="label_created")
    shipped_at: Mapped[str | None] = mapped_column(String, nullable=True)
    estimated_delivery_date: Mapped[str | None] = mapped_column(String, nullable=True)


class ExternalProduct(Base):
    __tablename__ = "external_products"

    external_product_id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    vendor_name: Mapped[str] = mapped_column(String, nullable=False)
    category: Mapped[str] = mapped_column(String, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    rating: Mapped[float] = mapped_column(Float, default=0.0)
    image_url: Mapped[str] = mapped_column(String, default="")
    tags: Mapped[str] = mapped_column(String, default="")
    affiliate_link_template: Mapped[str] = mapped_column(String, nullable=False)


class AffiliateClick(Base):
    __tablename__ = "affiliate_clicks"

    click_id: Mapped[str] = mapped_column(
        String, primary_key=True, default=lambda: str(uuid.uuid4())
    )
    external_product_id: Mapped[str] = mapped_column(
        ForeignKey("external_products.external_product_id")
    )
    session_id: Mapped[str] = mapped_column(String, nullable=True)
    clicked_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )
    conversion_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)


class Promotion(Base):
    """A discount applying to a specific product OR an entire category.
    Used by the recommendation ranking pipeline to boost/flag discounted
    items — not a full promo-code/cart-rules engine, just enough to
    demonstrate promotion-aware ranking."""
    __tablename__ = "promotions"

    promotion_id: Mapped[str] = mapped_column(
        String, primary_key=True, default=lambda: str(uuid.uuid4())
    )
    product_id: Mapped[str] = mapped_column(String, nullable=True)   # specific product, or...
    category: Mapped[str] = mapped_column(String, nullable=True)     # ...applies to whole category
    discount_percent: Mapped[float] = mapped_column(Float, nullable=False)
    label: Mapped[str] = mapped_column(String, default="")            # e.g. "Summer Sale"
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class ChatFeedback(Base):
    __tablename__ = "chat_feedback"

    feedback_id: Mapped[str] = mapped_column(
        String, primary_key=True, default=lambda: str(uuid.uuid4())
    )
    session_id: Mapped[str] = mapped_column(String, nullable=False)
    message_index: Mapped[int] = mapped_column(Integer, nullable=False)
    rating: Mapped[str] = mapped_column(String, nullable=False)
    user_message: Mapped[str] = mapped_column(Text, default="")
    assistant_reply: Mapped[str] = mapped_column(Text, default="")
    products_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )
