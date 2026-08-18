from sqlalchemy.orm import Session

from scout.db.models import RecommendationFeedback
from scout.db.session import SessionLocal, engine
from scout.services.cache import clear_cache

SUPPORTED_RECOMMENDATION_FEEDBACK = {"up", "down"}
FEEDBACK_RANKING_WEIGHT = 0.03

_feedback_table_ready = False


def _ensure_feedback_table() -> None:
    global _feedback_table_ready
    if _feedback_table_ready:
        return
    RecommendationFeedback.__table__.create(bind=engine, checkfirst=True)
    _feedback_table_ready = True


def _normalized_actor(session_id: str | None, customer_id: str | None) -> tuple[str | None, str | None]:
    normalized_session_id = session_id.strip() if isinstance(session_id, str) and session_id.strip() else None
    normalized_customer_id = customer_id.strip() if isinstance(customer_id, str) and customer_id.strip() else None
    return normalized_session_id, normalized_customer_id


def record_recommendation_feedback(
    *,
    product_id: str,
    rating: str,
    session_id: str | None = None,
    customer_id: str | None = None,
    recommendation_id: str | None = None,
) -> RecommendationFeedback:
    normalized_rating = rating.strip().lower()
    if normalized_rating not in SUPPORTED_RECOMMENDATION_FEEDBACK:
        raise ValueError("rating must be up or down")

    normalized_product_id = product_id.strip() if isinstance(product_id, str) else ""
    if not normalized_product_id:
        raise ValueError("product_id is required")

    normalized_session_id, normalized_customer_id = _normalized_actor(session_id, customer_id)
    if not normalized_session_id and not normalized_customer_id:
        raise ValueError("session_id or customer_id is required")

    normalized_recommendation_id = (
        recommendation_id.strip()
        if isinstance(recommendation_id, str) and recommendation_id.strip()
        else None
    )

    _ensure_feedback_table()
    session = SessionLocal()
    try:
        feedback = _find_existing_feedback(
            session,
            product_id=normalized_product_id,
            session_id=normalized_session_id,
            customer_id=normalized_customer_id,
            recommendation_id=normalized_recommendation_id,
        )
        if feedback is None:
            feedback = RecommendationFeedback(
                session_id=normalized_session_id,
                customer_id=normalized_customer_id,
                recommendation_id=normalized_recommendation_id,
                product_id=normalized_product_id,
                rating=normalized_rating,
            )
            session.add(feedback)
        else:
            feedback.rating = normalized_rating
            if normalized_recommendation_id and not feedback.recommendation_id:
                feedback.recommendation_id = normalized_recommendation_id

        session.commit()
        session.refresh(feedback)
        clear_cache()
        return feedback
    finally:
        session.close()


def _find_existing_feedback(
    session: Session,
    *,
    product_id: str,
    session_id: str | None,
    customer_id: str | None,
    recommendation_id: str | None,
) -> RecommendationFeedback | None:
    query = session.query(RecommendationFeedback).filter(
        RecommendationFeedback.product_id == product_id
    )
    if customer_id:
        query = query.filter(RecommendationFeedback.customer_id == customer_id)
    else:
        query = query.filter(RecommendationFeedback.customer_id.is_(None))
    if session_id:
        query = query.filter(RecommendationFeedback.session_id == session_id)
    else:
        query = query.filter(RecommendationFeedback.session_id.is_(None))
    if recommendation_id:
        query = query.filter(
            (RecommendationFeedback.recommendation_id == recommendation_id)
            | (RecommendationFeedback.recommendation_id.is_(None))
        )
    return query.order_by(RecommendationFeedback.created_at.desc()).first()


def feedback_adjustment_for_product(
    session: Session,
    *,
    product_id: str,
    session_id: str | None = None,
    customer_id: str | None = None,
) -> float:
    normalized_session_id, normalized_customer_id = _normalized_actor(session_id, customer_id)
    if not normalized_session_id and not normalized_customer_id:
        return 0.0

    query = session.query(RecommendationFeedback).filter(
        RecommendationFeedback.product_id == product_id
    )
    if normalized_customer_id:
        query = query.filter(RecommendationFeedback.customer_id == normalized_customer_id)
    else:
        query = query.filter(RecommendationFeedback.session_id == normalized_session_id)

    latest = query.order_by(RecommendationFeedback.updated_at.desc()).first()
    if latest is None:
        return 0.0
    return FEEDBACK_RANKING_WEIGHT if latest.rating == "up" else -FEEDBACK_RANKING_WEIGHT


def list_recommendation_feedback(
    *,
    session_id: str | None = None,
    customer_id: str | None = None,
) -> list[dict]:
    normalized_session_id, normalized_customer_id = _normalized_actor(session_id, customer_id)
    if not normalized_session_id and not normalized_customer_id:
        return []

    _ensure_feedback_table()
    session = SessionLocal()
    try:
        query = session.query(RecommendationFeedback)
        if normalized_customer_id:
            query = query.filter(RecommendationFeedback.customer_id == normalized_customer_id)
        else:
            query = query.filter(RecommendationFeedback.session_id == normalized_session_id)
        rows = query.order_by(RecommendationFeedback.updated_at.desc()).all()
        return [
            {
                "feedback_id": row.feedback_id,
                "session_id": row.session_id,
                "customer_id": row.customer_id,
                "recommendation_id": row.recommendation_id,
                "product_id": row.product_id,
                "rating": row.rating,
                "created_at": row.created_at.isoformat(),
                "updated_at": row.updated_at.isoformat(),
            }
            for row in rows
        ]
    finally:
        session.close()
