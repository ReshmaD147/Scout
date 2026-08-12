import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from scout.api import chat as chat_api
from scout.db.models import ChatFeedback
from scout.services import chat_feedback_service


def _test_app():
    app = FastAPI()
    app.include_router(chat_api.router)
    return app


def test_chat_feedback_endpoint_records_local_demo_feedback(monkeypatch):
    recorded = {}

    def fake_record_chat_feedback(**kwargs):
        recorded.update(kwargs)
        return "feedback-1"

    monkeypatch.setattr(chat_api, "record_chat_feedback", fake_record_chat_feedback)

    with TestClient(_test_app()) as client:
        response = client.post(
            "/chat/feedback",
            json={
                "session_id": "sess-1",
                "message_index": 2,
                "rating": "up",
                "user_message": "Recommend a dress under $80",
                "assistant_reply": "I found 3 Scout dresses.",
                "products": [{"product_id": "P003", "name": "Wrap Dress", "price": 68.0}],
            },
        )

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "feedback_id": "feedback-1"}
    assert recorded == {
        "session_id": "sess-1",
        "message_index": 2,
        "rating": "up",
        "user_message": "Recommend a dress under $80",
        "assistant_reply": "I found 3 Scout dresses.",
        "products": [{"product_id": "P003", "name": "Wrap Dress", "price": 68.0}],
    }


def test_chat_feedback_endpoint_rejects_invalid_rating():
    with TestClient(_test_app()) as client:
        response = client.post(
            "/chat/feedback",
            json={
                "session_id": "sess-1",
                "message_index": 0,
                "rating": "maybe",
            },
        )

    assert response.status_code == 422


def test_chat_feedback_endpoint_rejects_negative_message_index():
    with TestClient(_test_app()) as client:
        response = client.post(
            "/chat/feedback",
            json={
                "session_id": "sess-1",
                "message_index": -1,
                "rating": "down",
            },
        )

    assert response.status_code == 422


def test_record_chat_feedback_writes_sanitized_products_to_sqlite(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'feedback.db'}",
        connect_args={"check_same_thread": False},
    )
    session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    monkeypatch.setattr(chat_feedback_service, "engine", engine)
    monkeypatch.setattr(chat_feedback_service, "SessionLocal", session_factory)
    monkeypatch.setattr(chat_feedback_service, "_feedback_table_ready", False)

    feedback_id = chat_feedback_service.record_chat_feedback(
        session_id="sess-2",
        message_index=4,
        rating="down",
        user_message="hello" * 1200,
        assistant_reply="reply" * 2500,
        products=[
            {
                "product_id": "P003",
                "name": "Wrap Dress",
                "source": "internal",
                "price": 68.0,
                "raw_tool_result": {"do_not_store": True},
            }
        ],
    )

    session = session_factory()
    try:
        row = session.get(ChatFeedback, feedback_id)
        assert row is not None
        assert row.session_id == "sess-2"
        assert row.message_index == 4
        assert row.rating == "down"
        assert len(row.user_message) == 4000
        assert len(row.assistant_reply) == 8000
        assert json.loads(row.products_json) == [
            {
                "product_id": "P003",
                "name": "Wrap Dress",
                "source": "internal",
                "price": 68.0,
            }
        ]
        assert "raw_tool_result" not in row.products_json
    finally:
        session.close()
