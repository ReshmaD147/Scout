import { useState, useEffect } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { loadStripe } from "@stripe/stripe-js";
import { Elements } from "@stripe/react-stripe-js";
import { useCart } from "../context/CartContext";
import CheckoutForm from "../components/CheckoutForm";
import "./CartPage.css";

const API_BASE = import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8000";

function resolveImageUrl(url) {
  if (!url) return url;
  return url.startsWith("/") ? `${API_BASE}${url}` : url;
}
const stripePromise = loadStripe(import.meta.env.VITE_STRIPE_PUBLISHABLE_KEY);

export default function CartPage() {
  const { items, updateQuantity, removeFromCart, clearCart, total } = useCart();
  const [isStartingCheckout, setIsStartingCheckout] = useState(false);
  const [checkoutSession, setCheckoutSession] = useState(null);
  const [confirmation, setConfirmation] = useState(null);
  const [error, setError] = useState(null);
  const [searchParams, setSearchParams] = useSearchParams();
  const shippingLabel = total >= 75 ? "Free" : "Free over $75";
  const itemCount = items.reduce((sum, item) => sum + item.quantity, 0);

  useEffect(() => {
    if (searchParams.get("checkout") === "1" && items.length > 0 && !checkoutSession && !confirmation) {
      handleStartCheckout();
      const next = new URLSearchParams(searchParams);
      next.delete("checkout");
      setSearchParams(next, { replace: true });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function handleStartCheckout() {
    setIsStartingCheckout(true);
    setError(null);
    try {
      const response = await fetch(`${API_BASE}/checkout`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          items: items.map((i) => ({
            product_id: i.product_id,
            quantity: i.quantity,
            size: i.size,
            color: i.color,
          })),
        }),
      });
      const data = await response.json();

      if (!data.success) {
        setError(data.error || "Checkout failed. Please try again.");
        return;
      }

      setCheckoutSession({
        clientSecret: data.payment.client_secret,
        order: data.order,
      });
    } catch {
      setError("We couldn’t start checkout right now. Please try again.");
    } finally {
      setIsStartingCheckout(false);
    }
  }

  function handlePaymentSuccess(paymentIntent) {
    setConfirmation({
      order: checkoutSession.order,
      paymentIntent,
    });
    clearCart();
  }

  if (confirmation) {
    const orderDate = new Date(confirmation.order.created_at).toLocaleString();
    return (
      <div className="cart-confirmation">
        <p className="cart-confirmation-check">✓</p>
        <h2>Order confirmed!</h2>
        <p className="cart-confirmation-id">Order #{confirmation.order.order_id}</p>
        <p className="cart-confirmation-note">{orderDate}</p>

        <div className="cart-receipt">
          <h3 className="cart-receipt-title">Receipt</h3>
          {confirmation.order.items.map((item) => (
            <div className="cart-receipt-line" key={item.product_id}>
              <span>{item.name} × {item.quantity}</span>
              <span>${(item.price_at_purchase * item.quantity).toFixed(2)}</span>
            </div>
          ))}
          <div className="cart-receipt-line cart-receipt-total">
            <span>Total charged</span>
            <span>${confirmation.order.total.toFixed(2)}</span>
          </div>
        </div>

        <p className="cart-confirmation-note">
          Payment status: {confirmation.paymentIntent.status} (Stripe test mode)
        </p>
        <Link to="/" className="cart-empty-link">Continue shopping</Link>
      </div>
    );
  }

  if (checkoutSession) {
    return (
      <div className="cart-page">
        <div className="cart-payment-card">
          <p className="cart-eyebrow">Secure checkout</p>
          <h2 className="cart-title">Complete your order</h2>
          <p className="cart-payment-note">
            Your payment is handled by Stripe Elements outside the AI assistant path.
          </p>
          <div className="cart-payment-summary">
            <span>Order total</span>
            <strong>${checkoutSession.order.total.toFixed(2)}</strong>
          </div>
          <div className="cart-demo-card-note">
            <span>Demo mode</span>
            <code>4242 4242 4242 4242</code>
            <small>Use any future expiry and any CVC.</small>
          </div>
          <Elements stripe={stripePromise} options={{ clientSecret: checkoutSession.clientSecret }}>
            <CheckoutForm onSuccess={handlePaymentSuccess} />
          </Elements>
        </div>
      </div>
    );
  }

  if (items.length === 0) {
    return (
      <div className="cart-empty">
        <div className="cart-empty-icon" aria-hidden="true">🛒</div>
        <h2>Your cart is empty</h2>
        <p>Find something you love, then Lumi will keep checkout secure and deterministic.</p>
        <Link to="/" className="cart-empty-link">Continue shopping</Link>
      </div>
    );
  }

  return (
    <div className="cart-page">
      <div className="cart-header">
        <p className="cart-eyebrow">Shopping bag</p>
        <h2 className="cart-title">Your Cart</h2>
        <p className="cart-subtitle">
          {itemCount} {itemCount === 1 ? "item" : "items"} ready for secure checkout.
        </p>
      </div>

      <div className="cart-layout">
        <div className="cart-items-panel">
          <div className="cart-items">
            {items.map((item) => (
              <div key={item.cart_key || item.product_id} className="cart-item">
                <img className="cart-item-image" src={resolveImageUrl(item.image_url)} alt={item.name} />

                <div className="cart-item-details">
                  <p className="cart-item-brand">{item.brand}</p>
                  <p className="cart-item-name">{item.name}</p>
                  <p className="cart-item-price">${item.price.toFixed(2)}</p>
                  <p className="cart-item-note">
                    {item.size || item.color
                      ? [item.color, item.size ? `Size ${item.size}` : ""].filter(Boolean).join(" · ")
                      : "Size and color confirmed during checkout"}
                  </p>
                </div>

                <div className="cart-item-qty" aria-label={`Quantity for ${item.name}`}>
                  <button
                    className="cart-qty-btn"
                    onClick={() => updateQuantity(item.cart_key || item.product_id, item.quantity - 1)}
                    aria-label={`Decrease quantity for ${item.name}`}
                  >
                    −
                  </button>
                  <span className="cart-qty-value">{item.quantity}</span>
                  <button
                    className="cart-qty-btn"
                    onClick={() => updateQuantity(item.cart_key || item.product_id, item.quantity + 1)}
                    aria-label={`Increase quantity for ${item.name}`}
                  >
                    +
                  </button>
                </div>

                <p className="cart-item-subtotal">
                  ${(item.price * item.quantity).toFixed(2)}
                </p>

                <button
                  className="cart-item-remove"
                  onClick={() => removeFromCart(item.cart_key || item.product_id)}
                  aria-label={`Remove ${item.name}`}
                >
                  ✕
                </button>
              </div>
            ))}
          </div>

          <button className="cart-clear-btn" onClick={clearCart}>
            Clear cart
          </button>
        </div>

        <aside className="cart-summary" aria-label="Order summary">
          <h3>Order summary</h3>
          <div className="cart-summary-row">
            <span>Subtotal</span>
            <span>${total.toFixed(2)}</span>
          </div>
          <div className="cart-summary-row">
            <span>Shipping</span>
            <span>{shippingLabel}</span>
          </div>
          <div className="cart-summary-row">
            <span>Estimated tax</span>
            <span>Calculated at checkout</span>
          </div>
          <div className="cart-total-row">
            <span>Estimated total</span>
            <span className="cart-total-value">${total.toFixed(2)}</span>
          </div>

          {error && <p className="cart-error">{error}</p>}

          <button
            className="cart-checkout-btn"
            onClick={handleStartCheckout}
            disabled={isStartingCheckout}
          >
            {isStartingCheckout ? "Starting…" : "Proceed to checkout"}
          </button>

          <div className="cart-summary-trust">
            <span>Secure payment powered by Stripe</span>
            <span>Stock and price verified server-side</span>
            <span>30-day returns on eligible items</span>
          </div>
        </aside>
        </div>
    </div>
  );
}
