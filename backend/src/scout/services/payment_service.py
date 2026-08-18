import os

import stripe
from dotenv import load_dotenv

load_dotenv()

stripe.api_key = os.getenv("STRIPE_MCP_KEY")


class PaymentProcessingError(Exception):
    """Raised when Stripe itself fails to create a PaymentIntent —
    wraps the raw stripe.error.StripeError with a clearer message,
    so callers (checkout.py) can catch one specific, known exception
    type instead of needing to know about Stripe's internal error
    hierarchy.
    """


def create_test_payment(
    amount_usd: float,
    description: str = "Scout order",
    metadata: dict[str, str] | None = None,
) -> dict:
    """Creates a Stripe test-mode PaymentIntent for the given amount.
    Deterministic — no LLM reasoning needed to move money.

    The test-mode guard below is a structural safety check, not just a
    convention: even if a real (sk_live_) key were accidentally placed
    in the environment, this function refuses to run rather than
    silently processing a real charge. No AI agent has any path to
    calling this function at all — see CLAUDE.md "Security boundaries".

    confirm=False means the PaymentIntent is created but not confirmed
    here — actual confirmation happens client-side via Stripe Elements,
    so no money moves as a direct result of this call alone.

    Raises PaymentProcessingError (not Stripe's raw exception type) if
    Stripe itself fails — e.g. network issue, invalid amount, Stripe
    outage — so callers only need to handle one known exception type.

    NOTE: amount_cents uses float-to-int rounding, which can theoretically
    introduce sub-cent rounding errors for unusual inputs — acceptable
    for this project's scope, worth revisiting if this handled real
    production currency amounts at scale.
    """
    if not stripe.api_key or not stripe.api_key.startswith(("sk_test_", "rk_test_")):
        raise RuntimeError(
            "Stripe key is missing or not a test-mode key. "
            "Refusing to process a payment outside test mode."
        )

    amount_cents = int(round(amount_usd * 100))

    try:
        intent = stripe.PaymentIntent.create(
            amount=amount_cents,
            currency="usd",
            description=description,
            automatic_payment_methods={"enabled": True},
            confirm=False,
            metadata=metadata or {},
        )
    except stripe.error.StripeError as e:
        raise PaymentProcessingError(f"Payment could not be processed: {e.user_message or str(e)}") from e

    return {
        "payment_intent_id": intent.id,
        "amount_usd": amount_usd,
        "status": intent.status,
        "client_secret": intent.client_secret,
    }


def retrieve_test_payment(payment_intent_id: str) -> dict:
    if not stripe.api_key or not stripe.api_key.startswith(("sk_test_", "rk_test_")):
        raise RuntimeError(
            "Stripe key is missing or not a test-mode key. "
            "Refusing to process a payment outside test mode."
        )

    try:
        intent = stripe.PaymentIntent.retrieve(payment_intent_id)
    except stripe.error.StripeError as e:
        raise PaymentProcessingError(
            f"Payment could not be verified: {e.user_message or str(e)}"
        ) from e

    return {
        "payment_intent_id": intent.id,
        "amount": intent.amount,
        "amount_received": intent.amount_received,
        "currency": intent.currency,
        "status": intent.status,
        "metadata": dict(intent.metadata or {}),
    }
