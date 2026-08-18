import { useState, useEffect } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { loadStripe } from "@stripe/stripe-js";
import { Elements } from "@stripe/react-stripe-js";
import { useCart } from "../context/CartContext";
import { useAuth } from "../context/AuthContext";
import { useChatWidget } from "../context/ChatWidgetContext";
import CheckoutForm from "../components/CheckoutForm";
import { formatProductDisplayName } from "../components/productDisplay";
import "./CartPage.css";

const API_BASE = import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8000";

function resolveImageUrl(url) {
  if (!url) return url;
  return url.startsWith("/") ? `${API_BASE}${url}` : url;
}
const stripePromise = loadStripe(import.meta.env.VITE_STRIPE_PUBLISHABLE_KEY);

const EMPTY_ADDRESS = {
  full_name: "",
  address_line1: "",
  address_line2: "",
  city: "",
  state: "",
  postal_code: "",
  country: "US",
};

function AddressFields({ title, address, onChange }) {
  const update = (field, value) => onChange({ ...address, [field]: value });

  return (
    <fieldset className="cart-address-fieldset">
      <legend>{title}</legend>
      <label className="cart-field cart-field--full">
        <span>Full name</span>
        <input value={address.full_name} onChange={(e) => update("full_name", e.target.value)} autoComplete="name" />
      </label>
      <label className="cart-field cart-field--full">
        <span>Address line 1</span>
        <input value={address.address_line1} onChange={(e) => update("address_line1", e.target.value)} autoComplete="address-line1" />
      </label>
      <label className="cart-field cart-field--full">
        <span>Address line 2 <small>optional</small></span>
        <input value={address.address_line2} onChange={(e) => update("address_line2", e.target.value)} autoComplete="address-line2" />
      </label>
      <label className="cart-field">
        <span>City</span>
        <input value={address.city} onChange={(e) => update("city", e.target.value)} autoComplete="address-level2" />
      </label>
      <label className="cart-field">
        <span>State</span>
        <input value={address.state} onChange={(e) => update("state", e.target.value)} autoComplete="address-level1" />
      </label>
      <label className="cart-field">
        <span>ZIP / postal code</span>
        <input value={address.postal_code} onChange={(e) => update("postal_code", e.target.value)} autoComplete="postal-code" />
      </label>
      <label className="cart-field">
        <span>Country</span>
        <input value={address.country} onChange={(e) => update("country", e.target.value)} autoComplete="country" />
      </label>
    </fieldset>
  );
}

function cleanAddress(address) {
  return {
    full_name: address.full_name.trim(),
    address_line1: address.address_line1.trim(),
    address_line2: address.address_line2.trim() || null,
    city: address.city.trim(),
    state: address.state.trim(),
    postal_code: address.postal_code.trim(),
    country: address.country.trim() || "US",
  };
}

function formatAddress(address) {
  if (!address) return "";
  return [
    address.full_name,
    address.address_line1,
    address.address_line2,
    [address.city, address.state, address.postal_code].filter(Boolean).join(", "),
    address.country,
  ].filter(Boolean).join(" · ");
}

function formatShippingDestination(address) {
  if (!address) return "Shipping address unavailable";
  const cityState = [address.city, address.state].filter(Boolean).join(", ");
  return cityState || formatAddress(address);
}

export default function CartPage() {
  const { items, updateQuantity, removeFromCart, clearCart, total } = useCart();
  const { sessionId } = useAuth();
  const { chatSessionId } = useChatWidget();
  const checkoutSessionId = sessionId || chatSessionId;
  const [isStartingCheckout, setIsStartingCheckout] = useState(false);
  const [checkoutSession, setCheckoutSession] = useState(null);
  const [confirmation, setConfirmation] = useState(null);
  const [error, setError] = useState(null);
  const [contactEmail, setContactEmail] = useState("");
  const [shippingAddress, setShippingAddress] = useState(EMPTY_ADDRESS);
  const [billingSameAsShipping, setBillingSameAsShipping] = useState(true);
  const [billingAddress, setBillingAddress] = useState(EMPTY_ADDRESS);
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
      if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(contactEmail.trim())) {
        setError("Enter a valid email for your confirmation and Stripe receipt.");
        return;
      }
      const cleanedShippingAddress = cleanAddress(shippingAddress);
      const cleanedBillingAddress = billingSameAsShipping ? cleanedShippingAddress : cleanAddress(billingAddress);
      const requiredShippingFields = ["full_name", "address_line1", "city", "state", "postal_code", "country"];
      if (requiredShippingFields.some((field) => !cleanedShippingAddress[field])) {
        setError("Complete the shipping address before checkout.");
        return;
      }
      if (!billingSameAsShipping && requiredShippingFields.some((field) => !cleanedBillingAddress[field])) {
        setError("Complete the billing address or choose same as shipping.");
        return;
      }
      const checkoutItems = items.map((i) => ({
        product_id: i.product_id,
        quantity: i.quantity,
        size: i.size,
        color: i.color,
        attribution_source: i.attribution_source,
        recommendation_id: i.recommendation_id,
      }));
      const checkoutDetails = {
        contact_email: contactEmail.trim().toLowerCase(),
        shipping_address: cleanedShippingAddress,
        billing_same_as_shipping: billingSameAsShipping,
        billing_address: billingSameAsShipping ? cleanedShippingAddress : cleanedBillingAddress,
      };
      const response = await fetch(`${API_BASE}/checkout`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          items: checkoutItems,
          session_id: checkoutSessionId,
          ...checkoutDetails,
        }),
      });
      const data = await response.json();

      if (!data.success) {
        setError(data.error || "Checkout failed. Please try again.");
        return;
      }

      setCheckoutSession({
        clientSecret: data.payment.client_secret,
        total: data.total,
        items: checkoutItems,
        sessionId: checkoutSessionId,
        checkoutDetails,
      });
    } catch {
      setError("We couldn’t start checkout right now. Please try again.");
    } finally {
      setIsStartingCheckout(false);
    }
  }

  async function handlePaymentSuccess(paymentIntent) {
    const response = await fetch(`${API_BASE}/checkout/finalize`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        payment_intent_id: paymentIntent.id,
        items: checkoutSession.items,
        session_id: checkoutSession.sessionId,
        ...checkoutSession.checkoutDetails,
      }),
    });
    const data = await response.json();
    if (!response.ok || !data.success) {
      throw new Error(data.error || "Payment succeeded, but the order could not be finalized.");
    }
    setConfirmation({ order: data.order, paymentIntent });
    clearCart();
  }

  if (confirmation) {
    const confirmationEmail = confirmation.order.contact_email || confirmation.order.stripe_receipt_email;
    return (
      <div className="cart-confirmation">
        <div className="cart-confirmation-check" aria-hidden="true">✓</div>
        <h2>Order confirmed</h2>
        <p className="cart-confirmation-id">Order #{confirmation.order.order_id}</p>
        <div className="cart-confirmation-details">
          <p>
            <strong>Total</strong>
            <span>${confirmation.order.total.toFixed(2)}</span>
          </p>
          <p>
            <strong>Shipping to</strong>
            <span>{formatShippingDestination(confirmation.order.shipping_address)}</span>
          </p>
          <p>
            <strong>Confirmation sent to</strong>
            <span>
              {confirmationEmail || "Email not available"}
            </span>
          </p>
        </div>

        <div className="cart-receipt">
          <h3 className="cart-receipt-title">Receipt</h3>
          {confirmation.order.items.map((item) => {
            const displayName = formatProductDisplayName(item.name);
            return (
              <div className="cart-receipt-line" key={item.product_id}>
                <span>{displayName} × {item.quantity}</span>
                <span>${(item.price_at_purchase * item.quantity).toFixed(2)}</span>
              </div>
            );
          })}
          <div className="cart-receipt-line cart-receipt-total">
            <span>Total charged</span>
            <span>${confirmation.order.total.toFixed(2)}</span>
          </div>
        </div>

        <p className="cart-confirmation-note">
          Payment status: {confirmation.paymentIntent.status} · Stripe receipt handled by Stripe test mode.
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
            <strong>${checkoutSession.total.toFixed(2)}</strong>
          </div>
          <div className="cart-order-review">
            <h3>Review before payment</h3>
            <p><strong>Email</strong><span>{checkoutSession.checkoutDetails.contact_email}</span></p>
            <p><strong>Ship to</strong><span>{formatAddress(checkoutSession.checkoutDetails.shipping_address)}</span></p>
            <p><strong>Billing</strong><span>{checkoutSession.checkoutDetails.billing_same_as_shipping ? "Same as shipping" : formatAddress(checkoutSession.checkoutDetails.billing_address)}</span></p>
            {items.map((item) => (
              <p key={item.cart_key || item.product_id}>
                <strong>{formatProductDisplayName(item.name)}</strong>
                <span>{item.quantity} × ${item.price.toFixed(2)}</span>
              </p>
            ))}
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
        <p>Find something you love, then enjoy a secure, straightforward checkout.</p>
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
          <div className="cart-checkout-details">
            <h3>Checkout details</h3>
            <label className="cart-field cart-field--full">
              <span>Email for confirmation and Stripe receipt</span>
              <input
                type="email"
                value={contactEmail}
                onChange={(e) => setContactEmail(e.target.value)}
                autoComplete="email"
              />
            </label>
            <AddressFields title="Shipping address" address={shippingAddress} onChange={setShippingAddress} />
            <label className="cart-checkbox">
              <input
                type="checkbox"
                checked={billingSameAsShipping}
                onChange={(e) => setBillingSameAsShipping(e.target.checked)}
              />
              <span>Billing address is the same as shipping</span>
            </label>
            {!billingSameAsShipping && (
              <AddressFields title="Billing address" address={billingAddress} onChange={setBillingAddress} />
            )}
          </div>
          <div className="cart-items">
            {items.map((item) => {
              const displayName = formatProductDisplayName(item.name);
              return (
                <div key={item.cart_key || item.product_id} className="cart-item">
                  <img className="cart-item-image" src={resolveImageUrl(item.image_url)} alt={displayName} />

                  <div className="cart-item-details">
                    <p className="cart-item-brand">{item.brand}</p>
                    <p className="cart-item-name">{displayName}</p>
                    <p className="cart-item-price">${item.price.toFixed(2)}</p>
                    <p className="cart-item-note">
                      {item.size || item.color
                        ? [item.color, item.size ? `Size ${item.size}` : ""].filter(Boolean).join(" · ")
                        : "Size and color confirmed during checkout"}
                    </p>
                  </div>

                  <div className="cart-item-qty" aria-label={`Quantity for ${displayName}`}>
                    <button
                      className="cart-qty-btn"
                      onClick={() => updateQuantity(item.cart_key || item.product_id, item.quantity - 1)}
                      aria-label={`Decrease quantity for ${displayName}`}
                    >
                      −
                    </button>
                    <span className="cart-qty-value">{item.quantity}</span>
                    <button
                      className="cart-qty-btn"
                      onClick={() => updateQuantity(item.cart_key || item.product_id, item.quantity + 1)}
                      aria-label={`Increase quantity for ${displayName}`}
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
                    aria-label={`Remove ${displayName}`}
                  >
                    ✕
                  </button>
                </div>
              );
            })}
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
