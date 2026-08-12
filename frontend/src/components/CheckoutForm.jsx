import { useState } from "react";
import { PaymentElement, useStripe, useElements } from "@stripe/react-stripe-js";

export default function CheckoutForm({ onSuccess }) {
  const stripe = useStripe();
  const elements = useElements();
  const [isProcessing, setIsProcessing] = useState(false);
  const [error, setError] = useState(null);

  async function handleSubmit(e) {
    e.preventDefault();
    if (!stripe || !elements) return;

    setIsProcessing(true);
    setError(null);

    const { error: confirmError, paymentIntent } = await stripe.confirmPayment({
      elements,
      redirect: "if_required",
    });

    if (confirmError) {
      setError(confirmError.message);
      setIsProcessing(false);
      return;
    }

    onSuccess(paymentIntent);
  }

  return (
    <form onSubmit={handleSubmit} className="checkout-form">
      <PaymentElement />
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
