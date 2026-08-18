import { useState } from "react";
import { CardElement, useStripe, useElements } from "@stripe/react-stripe-js";

const CARD_ELEMENT_OPTIONS = {
  disableLink: true,
  hidePostalCode: false,
  style: {
    base: {
      color: "#2f2942",
      fontFamily: "Inter, system-ui, sans-serif",
      fontSize: "16px",
      "::placeholder": {
        color: "#a89dc0",
      },
    },
    invalid: {
      color: "#b23a2e",
    },
  },
};

function getPaymentErrorMessage(confirmError) {
  const message = confirmError?.message || "";

  if (message.toLowerCase().includes("load failed")) {
    return "Stripe could not finish loading the test card payment. Refresh checkout and try the demo card again.";
  }

  return message || "Payment could not be completed. Please check the card details and try again.";
}

export default function CheckoutForm({ clientSecret, onSuccess }) {
  const stripe = useStripe();
  const elements = useElements();
  const [isProcessing, setIsProcessing] = useState(false);
  const [error, setError] = useState(null);

  async function handleSubmit(e) {
    e.preventDefault();
    if (!stripe || !elements) return;

    setIsProcessing(true);
    setError(null);

    const card = elements.getElement(CardElement);
    if (!card) {
      setError("Card details are not ready yet. Please try again.");
      setIsProcessing(false);
      return;
    }

    const { error: confirmError, paymentIntent } = await stripe.confirmCardPayment(clientSecret, {
      payment_method: {
        card,
      },
    });

    if (confirmError) {
      setError(getPaymentErrorMessage(confirmError));
      setIsProcessing(false);
      return;
    }

    if (!paymentIntent) {
      setError("Payment needs another step that this demo checkout does not support. Please use the test card instead.");
      setIsProcessing(false);
      return;
    }

    try {
      await onSuccess(paymentIntent);
    } catch (finalizeError) {
      setError(finalizeError.message || "Payment succeeded, but the order could not be finalized.");
      setIsProcessing(false);
    }
  }

  return (
    <form onSubmit={handleSubmit} className="checkout-form">
      <div className="cart-card-element">
        <CardElement options={CARD_ELEMENT_OPTIONS} />
      </div>
      {error && <p className="cart-error">{error}</p>}
      <button
        type="submit"
        className="cart-checkout-btn"
        disabled={!stripe || isProcessing}
        style={{ marginTop: "16px" }}
      >
        {isProcessing ? "Processing…" : "Pay now"}
      </button>
    </form>
  );
}
