from types import SimpleNamespace

from scout.services import payment_service


def test_create_test_payment_uses_card_only_payment_element(monkeypatch):
    captured = {}

    class FakePaymentIntent:
        @staticmethod
        def create(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                id="pi_test",
                amount=6799,
                status="requires_payment_method",
                client_secret="pi_test_secret",
            )

    monkeypatch.setattr(payment_service.stripe, "api_key", "sk_test_demo")
    monkeypatch.setattr(payment_service.stripe, "PaymentIntent", FakePaymentIntent)

    result = payment_service.create_test_payment(
        amount_usd=67.99,
        description="Scout order OTEST",
        metadata={"order_id": "OTEST"},
        receipt_email="demo@example.com",
    )

    assert result["payment_intent_id"] == "pi_test"
    assert captured["payment_method_types"] == ["card"]
    assert "automatic_payment_methods" not in captured
    assert captured["receipt_email"] == "demo@example.com"


def test_retrieve_test_payment_normalizes_stripe_metadata_object(monkeypatch):
    class FakeStripeMetadata:
        def to_dict_recursive(self):
            return {
                "order_id": "OTEST",
                "cart_fingerprint": "fingerprint",
            }

    class FakePaymentIntent:
        @staticmethod
        def retrieve(payment_intent_id):
            return SimpleNamespace(
                id=payment_intent_id,
                amount=7865,
                amount_received=7865,
                currency="usd",
                status="succeeded",
                metadata=FakeStripeMetadata(),
                receipt_email="demo@example.com",
            )

    monkeypatch.setattr(payment_service.stripe, "api_key", "sk_test_demo")
    monkeypatch.setattr(payment_service.stripe, "PaymentIntent", FakePaymentIntent)

    result = payment_service.retrieve_test_payment("pi_test")

    assert result["payment_intent_id"] == "pi_test"
    assert result["metadata"] == {
        "order_id": "OTEST",
        "cart_fingerprint": "fingerprint",
    }
    assert result["receipt_email"] == "demo@example.com"
