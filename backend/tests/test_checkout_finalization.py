from types import SimpleNamespace

from scout.agents.attribution import register_recommendation, validate_recommendation
from scout.api import checkout as checkout_api


class FakeSession:
    def close(self):
        pass


def _item():
    return checkout_api.CheckoutItem(product_id="P001", quantity=1)


def _attributed_item(recommendation_id="rec_test", recommendation_session_id="sess_test"):
    return checkout_api.CheckoutItem(
        product_id="P001",
        quantity=1,
        attribution_source="scout",
        recommendation_id=recommendation_id,
        recommendation_session_id=recommendation_session_id,
    )


def _cart_items():
    return [{
        "product_id": "P001",
        "quantity": 1,
        "attribution_source": None,
        "recommendation_id": None,
        "_computed_price": 67.99,
    }]


def _payment_metadata(session_id=None, items=None):
    return {
        "order_id": "OTEST",
        "cart_fingerprint": checkout_api._cart_fingerprint(
            items or [_item()],
            session_id=session_id,
        ),
    }


def test_checkout_start_creates_payment_intent_without_creating_order(monkeypatch):
    monkeypatch.setattr(checkout_api, "SessionLocal", FakeSession)
    monkeypatch.setattr(
        checkout_api,
        "_validated_cart_items",
        lambda session, items, recommendation_session_id=None: (_cart_items(), None),
    )
    monkeypatch.setattr(
        checkout_api,
        "create_test_payment",
        lambda **kwargs: {
            "payment_intent_id": "pi_test",
            "client_secret": "secret",
            "status": "requires_payment_method",
        },
    )

    def fail_if_called(*args, **kwargs):
        raise AssertionError("checkout initiation must not create an order")

    monkeypatch.setattr(checkout_api, "create_order", fail_if_called)

    result = checkout_api.checkout(checkout_api.CheckoutRequest(items=[_item()]))

    assert result["success"] is True
    assert result["total"] == 67.99
    assert "order" not in result


def test_recommendation_validation_is_bound_to_registered_session():
    recommendation_id = register_recommendation("P001", "sess_owner")

    assert validate_recommendation(recommendation_id, "P001", "sess_owner") is True
    assert validate_recommendation(recommendation_id, "P001", "sess_other") is False


def test_cart_fingerprint_binds_attribution_fields():
    recommendation_id = register_recommendation("P001", "sess_attr")

    assert checkout_api._cart_fingerprint([_item()]) != checkout_api._cart_fingerprint(
        [_attributed_item(recommendation_id, "sess_attr")],
        session_id="sess_attr",
    )


def test_checkout_validation_uses_request_session_not_item_session(monkeypatch):
    recommendation_id = register_recommendation("P001", "sess_owner")

    class FakeProductRepository:
        def __init__(self, session):
            pass

        def get_by_id(self, product_id):
            return SimpleNamespace(product_id=product_id, name="Black Midi Dress", price=67.99)

    monkeypatch.setattr(checkout_api, "ProductRepository", FakeProductRepository)
    monkeypatch.setattr(
        checkout_api,
        "check_stock",
        lambda session, product_id, size=None, color=None: {
            "in_stock": True,
            "total_quantity": 3,
            "requested_color": color,
            "requested_size": size,
        },
    )
    monkeypatch.setattr(checkout_api, "_get_active_promotion_dict", lambda session, product: None)

    cart_items, error = checkout_api._validated_cart_items(
        FakeSession(),
        [_attributed_item(recommendation_id, "sess_other")],
        recommendation_session_id="sess_owner",
    )

    assert error is None
    assert cart_items[0]["attribution_source"] == "scout"
    assert cart_items[0]["recommendation_id"] == recommendation_id
    assert "recommendation_session_id" not in cart_items[0]


def test_checkout_validation_rejects_cross_request_session_attribution(monkeypatch):
    recommendation_id = register_recommendation("P001", "sess_owner")

    class FakeProductRepository:
        def __init__(self, session):
            pass

        def get_by_id(self, product_id):
            return SimpleNamespace(product_id=product_id, name="Black Midi Dress", price=67.99)

    monkeypatch.setattr(checkout_api, "ProductRepository", FakeProductRepository)
    monkeypatch.setattr(
        checkout_api,
        "check_stock",
        lambda session, product_id, size=None, color=None: {
            "in_stock": True,
            "total_quantity": 3,
            "requested_color": color,
            "requested_size": size,
        },
    )
    monkeypatch.setattr(checkout_api, "_get_active_promotion_dict", lambda session, product: None)

    cart_items, error = checkout_api._validated_cart_items(
        FakeSession(),
        [_attributed_item(recommendation_id, "sess_owner")],
        recommendation_session_id="sess_other",
    )

    assert error is None
    assert cart_items[0]["attribution_source"] is None
    assert cart_items[0]["recommendation_id"] is None


def test_finalize_rejects_unconfirmed_payment_without_creating_order(monkeypatch):
    monkeypatch.setattr(checkout_api, "SessionLocal", FakeSession)
    monkeypatch.setattr(
        checkout_api,
        "retrieve_test_payment",
        lambda payment_intent_id: {
            "payment_intent_id": payment_intent_id,
            "amount": 6799,
            "amount_received": 0,
            "currency": "usd",
            "status": "requires_payment_method",
            "metadata": _payment_metadata(),
        },
    )

    def fail_if_called(*args, **kwargs):
        raise AssertionError("unconfirmed payment must not create an order")

    monkeypatch.setattr(checkout_api, "create_order", fail_if_called)

    result = checkout_api.finalize_checkout(
        checkout_api.CheckoutFinalizeRequest(
            payment_intent_id="pi_test",
            items=[_item()],
        )
    )

    assert result == {
        "success": False,
        "error": "Payment has not completed successfully.",
    }


def test_finalize_rejects_attribution_changed_after_payment_started(monkeypatch):
    recommendation_id = register_recommendation("P001", "sess_attr")
    monkeypatch.setattr(checkout_api, "SessionLocal", FakeSession)
    monkeypatch.setattr(
        checkout_api,
        "retrieve_test_payment",
        lambda payment_intent_id: {
            "payment_intent_id": payment_intent_id,
            "amount": 6799,
            "amount_received": 6799,
            "currency": "usd",
            "status": "succeeded",
            "metadata": _payment_metadata(),
        },
    )

    def fail_if_called(*args, **kwargs):
        raise AssertionError("mismatched checkout must not create an order")

    monkeypatch.setattr(checkout_api, "create_order", fail_if_called)

    result = checkout_api.finalize_checkout(
        checkout_api.CheckoutFinalizeRequest(
            payment_intent_id="pi_test",
            items=[_attributed_item(recommendation_id, "sess_attr")],
        )
    )

    assert result == {
        "success": False,
        "error": "Checkout items do not match the completed payment.",
    }


def test_finalize_uses_request_session_for_attribution(monkeypatch):
    recommendation_id = register_recommendation("P001", "sess_owner")
    checkout_item = _attributed_item(recommendation_id, "sess_attacker")
    monkeypatch.setattr(checkout_api, "SessionLocal", FakeSession)
    monkeypatch.setattr(
        checkout_api,
        "retrieve_test_payment",
        lambda payment_intent_id: {
            "payment_intent_id": payment_intent_id,
            "amount": 6799,
            "amount_received": 6799,
            "currency": "usd",
            "status": "succeeded",
            "metadata": _payment_metadata(session_id="sess_owner", items=[checkout_item]),
        },
    )
    monkeypatch.setattr(checkout_api, "get_order", lambda session, order_id: None)

    class FakeProductRepository:
        def __init__(self, session):
            pass

        def get_by_id(self, product_id):
            return SimpleNamespace(product_id=product_id, name="Black Midi Dress", price=67.99)

    monkeypatch.setattr(checkout_api, "ProductRepository", FakeProductRepository)
    monkeypatch.setattr(
        checkout_api,
        "check_stock",
        lambda session, product_id, size=None, color=None: {
            "in_stock": True,
            "total_quantity": 3,
            "requested_color": color,
            "requested_size": size,
        },
    )
    monkeypatch.setattr(checkout_api, "_get_active_promotion_dict", lambda session, product: None)
    captured = {}

    def fake_create_order(session, **kwargs):
        captured.update(kwargs)
        return {"order_id": kwargs["order_id"], "status": kwargs["status"], "items": [{}]}

    monkeypatch.setattr(checkout_api, "create_order", fake_create_order)

    result = checkout_api.finalize_checkout(
        checkout_api.CheckoutFinalizeRequest(
            payment_intent_id="pi_test",
            items=[checkout_item],
            session_id="sess_owner",
        )
    )

    assert result["success"] is True
    assert captured["cart_items"][0]["attribution_source"] == "scout"
    assert captured["cart_items"][0]["recommendation_id"] == recommendation_id
    assert "recommendation_session_id" not in captured["cart_items"][0]


def test_finalize_creates_processing_order_after_verified_payment(monkeypatch):
    monkeypatch.setattr(checkout_api, "SessionLocal", FakeSession)
    monkeypatch.setattr(
        checkout_api,
        "retrieve_test_payment",
        lambda payment_intent_id: {
            "payment_intent_id": payment_intent_id,
            "amount": 6799,
            "amount_received": 6799,
            "currency": "usd",
            "status": "succeeded",
            "metadata": _payment_metadata(),
        },
    )
    monkeypatch.setattr(checkout_api, "get_order", lambda session, order_id: None)
    monkeypatch.setattr(
        checkout_api,
        "_validated_cart_items",
        lambda session, items, recommendation_session_id=None: (_cart_items(), None),
    )
    captured = {}

    def fake_create_order(session, **kwargs):
        captured.update(kwargs)
        return {"order_id": kwargs["order_id"], "status": kwargs["status"], "items": [{}]}

    monkeypatch.setattr(checkout_api, "create_order", fake_create_order)

    result = checkout_api.finalize_checkout(
        checkout_api.CheckoutFinalizeRequest(
            payment_intent_id="pi_test",
            items=[_item()],
        )
    )

    assert result["success"] is True
    assert captured["order_id"] == "OTEST"
    assert captured["status"] == "processing"
    assert "_computed_price" not in captured["cart_items"][0]
