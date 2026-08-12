import json

from scout.db.models import ChatFeedback
from scout.db.session import SessionLocal, engine

_feedback_table_ready = False


def _ensure_feedback_table() -> None:
    global _feedback_table_ready
    if _feedback_table_ready:
        return
    ChatFeedback.__table__.create(bind=engine, checkfirst=True)
    _feedback_table_ready = True


def _safe_products_json(products: list[dict]) -> str:
    safe_products = []
    for product in products[:10]:
        if not isinstance(product, dict):
            continue
        safe_products.append(
            {
                key: product.get(key)
                for key in (
                    "product_id",
                    "external_product_id",
                    "name",
                    "source",
                    "vendor_name",
                    "price",
                )
                if product.get(key) is not None
            }
        )
    return json.dumps(safe_products, ensure_ascii=False)


def record_chat_feedback(
    *,
    session_id: str,
    message_index: int,
    rating: str,
    user_message: str,
    assistant_reply: str,
    products: list[dict],
) -> str:
    _ensure_feedback_table()
    session = SessionLocal()
    try:
        feedback = ChatFeedback(
            session_id=session_id,
            message_index=message_index,
            rating=rating,
            user_message=user_message[:4000],
            assistant_reply=assistant_reply[:8000],
            products_json=_safe_products_json(products),
        )
        session.add(feedback)
        session.commit()
        session.refresh(feedback)
        return feedback.feedback_id
    finally:
        session.close()
