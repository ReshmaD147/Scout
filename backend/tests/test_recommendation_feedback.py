from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from scout.db.models import Base, Product, RecommendationFeedback, Stock
from scout.services import recommendation_feedback_service
from scout.services.cache import clear_cache
from scout.services.ranking_service import rank_products


def _temp_session_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'recommendation_feedback.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    return engine, sessionmaker(autocommit=False, autoflush=False, bind=engine)


def test_record_recommendation_feedback_saves_structured_row(tmp_path, monkeypatch):
    engine, session_factory = _temp_session_factory(tmp_path)
    monkeypatch.setattr(recommendation_feedback_service, "engine", engine)
    monkeypatch.setattr(recommendation_feedback_service, "SessionLocal", session_factory)
    monkeypatch.setattr(recommendation_feedback_service, "_feedback_table_ready", False)

    feedback = recommendation_feedback_service.record_recommendation_feedback(
        product_id="P001",
        recommendation_id="rec-1",
        session_id="sess-1",
        customer_id="C001",
        rating="up",
    )

    session = session_factory()
    try:
        row = session.get(RecommendationFeedback, feedback.feedback_id)
        assert row is not None
        assert row.product_id == "P001"
        assert row.recommendation_id == "rec-1"
        assert row.session_id == "sess-1"
        assert row.customer_id == "C001"
        assert row.rating == "up"
    finally:
        session.close()


def test_record_recommendation_feedback_switches_existing_rating(tmp_path, monkeypatch):
    engine, session_factory = _temp_session_factory(tmp_path)
    monkeypatch.setattr(recommendation_feedback_service, "engine", engine)
    monkeypatch.setattr(recommendation_feedback_service, "SessionLocal", session_factory)
    monkeypatch.setattr(recommendation_feedback_service, "_feedback_table_ready", False)

    first = recommendation_feedback_service.record_recommendation_feedback(
        product_id="P001",
        recommendation_id="rec-1",
        session_id="sess-1",
        rating="up",
    )
    second = recommendation_feedback_service.record_recommendation_feedback(
        product_id="P001",
        recommendation_id="rec-1",
        session_id="sess-1",
        rating="down",
    )

    session = session_factory()
    try:
        rows = session.query(RecommendationFeedback).all()
        assert len(rows) == 1
        assert first.feedback_id == second.feedback_id
        assert rows[0].rating == "down"
    finally:
        session.close()


def test_feedback_never_overrides_explicit_max_price_constraint(tmp_path):
    _, session_factory = _temp_session_factory(tmp_path)
    session = session_factory()
    try:
        session.add_all(
            [
                Product(
                    product_id="P001",
                    name="Liked Expensive Dress",
                    brand="Lumi",
                    department="Women",
                    category="dresses",
                    description="A dress above budget.",
                    price=120.0,
                    rating=5.0,
                    tags="dress,black",
                ),
                Product(
                    product_id="P002",
                    name="Budget Dress",
                    brand="Lumi",
                    department="Women",
                    category="dresses",
                    description="A dress under budget.",
                    price=60.0,
                    rating=4.0,
                    tags="dress,black",
                ),
                Stock(product_id="P001", size="M", color="black", quantity=5),
                Stock(product_id="P002", size="M", color="black", quantity=5),
                RecommendationFeedback(
                    product_id="P001",
                    session_id="sess-1",
                    rating="up",
                ),
            ]
        )
        session.commit()
        clear_cache()

        results = rank_products(
            session,
            candidate_product_ids=["P001", "P002"],
            target_price=80.0,
            max_price=80.0,
            top_n=2,
            session_id="sess-1",
        )

        assert [product["product_id"] for product in results] == ["P002"]
    finally:
        session.close()
