import asyncio

import pytest
from fastapi import HTTPException

from scout.agents import supervisor
from scout.agents.intent_splitter import SplitIntentResult, StructuredIntent
from scout.api import chat as chat_api
from scout.config import settings
from scout.db.session import SessionLocal
from scout.mcp_server import server as mcp_server
from scout.services.order_service import (
    AUTH_REQUIRED_ERROR,
    UNAUTHORIZED_ORDER_ERROR,
    check_return_eligibility_for_customer,
    get_order_for_customer,
    list_orders_for_authenticated_customer,
)


class App:
    scout_specialists = {"order_agent": object()}

    async def ainvoke(self, payload, config):
        raise AssertionError("order auth tests should not invoke the supervisor graph")


def _order_status_split(message):
    return SplitIntentResult(
        sub_intents=[message],
        structured_intent=StructuredIntent(
            text=message,
            request_type="order_status",
            confidence=0.96,
            order_id="O1001",
        ),
        fast_path_used=True,
        llm_splitter_invoked=False,
    )


def _enable_demo_auth(monkeypatch):
    monkeypatch.setattr(settings, "APP_ENV", "development")
    monkeypatch.setattr(settings, "ENABLE_DEMO_AUTH", True)
    monkeypatch.setattr(settings, "DEMO_CUSTOMER_IDS", "C001,C002")


def test_order_lookup_requires_authenticated_customer():
    session = SessionLocal()
    try:
        result = get_order_for_customer(session, "O1001", authenticated_customer_id=None)
    finally:
        session.close()

    assert result["authorized"] is False
    assert result["error_code"] == AUTH_REQUIRED_ERROR
    assert result["order_id"] == "O1001"
    assert "status" not in result
    assert "items" not in result


def test_order_lookup_allows_owner_only():
    session = SessionLocal()
    try:
        result = get_order_for_customer(session, "O1001", authenticated_customer_id="C001")
    finally:
        session.close()

    assert result["authorized"] is True
    assert result["found"] is True
    assert result["order_id"] == "O1001"
    assert result["customer_id"] == "C001"
    assert result["status"] == "shipped"
    assert result["items"]


def test_order_lookup_rejects_different_customer_without_exposing_data():
    session = SessionLocal()
    try:
        result = get_order_for_customer(session, "O1001", authenticated_customer_id="C002")
    finally:
        session.close()

    assert result["authorized"] is False
    assert result["error_code"] == UNAUTHORIZED_ORDER_ERROR
    assert result["order_id"] == "O1001"
    assert "status" not in result
    assert "items" not in result
    assert "customer_id" not in result


def test_order_history_requires_authenticated_customer():
    session = SessionLocal()
    try:
        result = list_orders_for_authenticated_customer(session, authenticated_customer_id=None)
    finally:
        session.close()

    assert result["authorized"] is False
    assert result["error_code"] == AUTH_REQUIRED_ERROR
    assert "orders" not in result


def test_order_history_returns_only_authenticated_customers_orders():
    session = SessionLocal()
    try:
        result = list_orders_for_authenticated_customer(session, authenticated_customer_id="C001")
    finally:
        session.close()

    assert result["authorized"] is True
    assert result["customer_id"] == "C001"
    assert {order["order_id"] for order in result["orders"]} == {"O1001", "O1003"}
    assert all(order["customer_id"] == "C001" for order in result["orders"])


def test_return_eligibility_requires_order_owner():
    session = SessionLocal()
    try:
        unauthenticated = check_return_eligibility_for_customer(session, "O1001", authenticated_customer_id=None)
        wrong_customer = check_return_eligibility_for_customer(session, "O1001", authenticated_customer_id="C002")
        owner = check_return_eligibility_for_customer(session, "O1001", authenticated_customer_id="C001")
    finally:
        session.close()

    assert unauthenticated["authorized"] is False
    assert unauthenticated["error_code"] == AUTH_REQUIRED_ERROR
    assert wrong_customer["authorized"] is False
    assert wrong_customer["error_code"] == UNAUTHORIZED_ORDER_ERROR
    assert owner["found"] is True
    assert owner["order_id"] == "O1001"
    assert owner["status"] == "shipped"
    assert "likely_eligible" in owner


def test_agent_order_tools_fail_closed_without_authenticated_identity():
    order = mcp_server.orders("O1001")
    history = mcp_server.order_history()
    eligibility = mcp_server.return_eligibility("O1001")

    assert order["authorized"] is False
    assert history["authorized"] is False
    assert eligibility["authorized"] is False
    assert order["error_code"] == AUTH_REQUIRED_ERROR
    assert history["error_code"] == AUTH_REQUIRED_ERROR
    assert eligibility["error_code"] == AUTH_REQUIRED_ERROR


def test_demo_auth_sign_in_creates_trusted_session_context(monkeypatch):
    _enable_demo_auth(monkeypatch)
    chat_api.SESSION_CONTEXTS.clear()
    chat_api.SESSION_HISTORIES.clear()

    response = asyncio.run(chat_api.demo_auth_sign_in(chat_api.DemoAuthRequest(customer_id="C001")))

    assert response.authenticated_customer_id == "C001"
    assert response.demo_auth_enabled is True
    assert chat_api.SESSION_CONTEXTS[response.session_id]["authenticated_customer_id"] == "C001"
    assert chat_api.SESSION_CONTEXTS[response.session_id]["demo_authenticated"] is True


def test_account_summary_requires_authenticated_demo_session():
    chat_api.SESSION_CONTEXTS.clear()

    with pytest.raises(HTTPException) as exc:
        asyncio.run(chat_api.account_summary(chat_api.AccountSummaryRequest(session_id="anon-session")))

    assert exc.value.status_code == 401


def test_account_summary_returns_orders_shipments_and_feedback(monkeypatch):
    chat_api.SESSION_CONTEXTS.clear()
    chat_api.SESSION_CONTEXTS["account-session"] = {
        "authenticated_customer_id": "C001",
        "demo_authenticated": True,
    }
    monkeypatch.setattr(
        chat_api,
        "list_recommendation_feedback",
        lambda **kwargs: [
            {
                "feedback_id": "fb-1",
                "session_id": kwargs["session_id"],
                "customer_id": kwargs["customer_id"],
                "recommendation_id": "rec-1",
                "product_id": "P001",
                "rating": "up",
                "created_at": "2026-08-18T00:00:00",
                "updated_at": "2026-08-18T00:00:00",
            }
        ],
    )

    summary = asyncio.run(chat_api.account_summary(chat_api.AccountSummaryRequest(session_id="account-session")))

    assert summary["customer"]["customer_id"] == "C001"
    assert summary["customer"]["demo_identity"] is True
    assert {order["order_id"] for order in summary["orders"]} == {"O1001", "O1003"}
    assert all(order["customer_id"] == "C001" for order in summary["orders"])
    assert all("shipment" in order for order in summary["orders"])
    assert summary["recommendation_feedback"][0]["product_id"] == "P001"
    assert summary["recommendation_feedback"][0]["product_name"] == "Black Midi Dress"


def test_authenticated_demo_owner_can_access_own_order(monkeypatch):
    _enable_demo_auth(monkeypatch)
    chat_api.SESSION_CONTEXTS.clear()

    response = asyncio.run(chat_api.demo_auth_sign_in(chat_api.DemoAuthRequest(customer_id="C001")))
    customer_id = chat_api.SESSION_CONTEXTS[response.session_id]["authenticated_customer_id"]
    token = mcp_server.set_authenticated_customer_id(customer_id)
    try:
        result = mcp_server.orders("O1001")
    finally:
        mcp_server.reset_authenticated_customer_id(token)

    assert result["authorized"] is True
    assert result["order_id"] == "O1001"
    assert result["customer_id"] == "C001"
    assert result["status"] == "shipped"


def test_authenticated_demo_customer_cannot_access_another_customers_order(monkeypatch):
    _enable_demo_auth(monkeypatch)
    chat_api.SESSION_CONTEXTS.clear()

    response = asyncio.run(chat_api.demo_auth_sign_in(chat_api.DemoAuthRequest(customer_id="C002")))
    customer_id = chat_api.SESSION_CONTEXTS[response.session_id]["authenticated_customer_id"]
    token = mcp_server.set_authenticated_customer_id(customer_id)
    try:
        result = mcp_server.orders("O1001")
    finally:
        mcp_server.reset_authenticated_customer_id(token)

    assert result["authorized"] is False
    assert result["error_code"] == UNAUTHORIZED_ORDER_ERROR
    assert "status" not in result
    assert "items" not in result


def test_unauthenticated_chat_still_fails_closed(monkeypatch):
    monkeypatch.setattr(supervisor, "get_chat_model", lambda: object())
    monkeypatch.setattr(supervisor, "_split_intents_for_supervisor", lambda model, message: _order_status_split(message))

    reply, _history, products = asyncio.run(
        supervisor.ask(App(), [], "Where is O1001?", conversation_context={})
    )

    assert "shipped" not in reply.lower()
    assert "O1001 has shipped" not in reply
    assert products == []


def test_customer_stated_id_does_not_bypass_authorization(monkeypatch):
    monkeypatch.setattr(supervisor, "get_chat_model", lambda: object())
    monkeypatch.setattr(supervisor, "_split_intents_for_supervisor", lambda model, message: _order_status_split(message))

    reply, _history, products = asyncio.run(
        supervisor.ask(App(), [], "My customer ID is C001. Where is O1001?", conversation_context={})
    )

    assert "shipped" not in reply.lower()
    assert "O1001 has shipped" not in reply
    assert products == []


def test_demo_auth_is_disabled_in_production(monkeypatch):
    monkeypatch.setattr(settings, "APP_ENV", "production")
    monkeypatch.setattr(settings, "ENABLE_DEMO_AUTH", True)
    monkeypatch.setattr(settings, "DEMO_CUSTOMER_IDS", "C001")

    with pytest.raises(HTTPException) as exc:
        asyncio.run(chat_api.demo_auth_sign_in(chat_api.DemoAuthRequest(customer_id="C001")))

    assert exc.value.status_code == 404
