import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useCart } from "../context/CartContext";
import "./CartToast.css";

const API_BASE = import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8000";

function resolveImageUrl(url) {
  if (!url) return url;
  return url.startsWith("/") ? `${API_BASE}${url}` : url;
}

export default function CartToast() {
  const { lastAdded, notifyId, itemCount, total, triggerElementRef } = useCart();
  const [visible, setVisible] = useState(false);
  const panelRef = useRef(null);
  const closeButtonRef = useRef(null);
  const navigate = useNavigate();

  // Close and, for explicit user-initiated closes, return focus to the
  // button that opened the toast (e.g. the "Add to cart" button clicked).
  const closeToast = useCallback(({ returnFocus } = { returnFocus: true }) => {
    setVisible(false);
    if (returnFocus && triggerElementRef?.current) {
      triggerElementRef.current.focus();
    }
  }, [triggerElementRef]);

  useEffect(() => {
    if (notifyId === 0) return;
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setVisible(true);
    // Auto-dismiss after 4s. Not treated as a user-initiated close, so
    // we don't forcibly move focus back — the user may already be doing
    // something else by then.
    const timer = setTimeout(() => setVisible(false), 4000);
    return () => clearTimeout(timer);
  }, [notifyId]);

  // Move focus into the panel when it opens.
  useEffect(() => {
    if (visible) {
      closeButtonRef.current?.focus();
    }
  }, [visible, closeToast]);

  // Close on Escape key.
  useEffect(() => {
    if (!visible) return;
    function handleKeyDown(e) {
      if (e.key === "Escape") {
        closeToast();
      }
    }
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [visible, closeToast]);

  // Close on outside click.
  useEffect(() => {
    if (!visible) return;
    function handleClickOutside(e) {
      if (panelRef.current && !panelRef.current.contains(e.target)) {
        closeToast({ returnFocus: false });
      }
    }
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, [visible, closeToast]);

  if (!visible || !lastAdded) return null;

  function handleViewCart() {
    closeToast({ returnFocus: false });
    navigate("/cart");
  }

  function handleCheckout() {
    closeToast({ returnFocus: false });
    // No separate /checkout route exists — checkout happens inline on
    // CartPage via its existing handleStartCheckout(). This query param
    // tells CartPage to auto-trigger that real flow on arrival, rather
    // than duplicating checkout logic here or through the AI agent.
    navigate("/cart?checkout=1");
  }

  return (
    <div className="cart-toast-overlay">
      <div
        className="cart-toast"
        role="dialog"
        aria-modal="true"
        aria-label="Added to cart"
        ref={panelRef}
      >
        <div className="cart-toast-header">
          <span className="cart-toast-check">✓</span>
          <span className="cart-toast-title">Added to cart!</span>
          <button
            className="cart-toast-close"
            onClick={() => closeToast()}
            aria-label="Close"
            ref={closeButtonRef}
          >
            ✕
          </button>
        </div>

        <div className="cart-toast-body">
          {lastAdded.image_url ? (
            <img
              className="cart-toast-image"
              src={resolveImageUrl(lastAdded.image_url)}
              alt={lastAdded.name}
            />
          ) : (
            <div className="cart-toast-image cart-toast-image--placeholder" />
          )}
          <div className="cart-toast-details">
            <p className="cart-toast-name">{lastAdded.name}</p>
            {/* Size only shown if it actually exists — no variant-selection
                UI exists yet, so this line simply won't render today. */}
            {lastAdded.size && (
              <p className="cart-toast-variant">
                Size: {lastAdded.size} · Qty: {lastAdded.quantity}
              </p>
            )}
            {!lastAdded.size && lastAdded.quantity > 1 && (
              <p className="cart-toast-variant">Qty: {lastAdded.quantity}</p>
            )}
            <p className="cart-toast-price">${lastAdded.price?.toFixed(2)}</p>
          </div>
        </div>

        <div className="cart-toast-footer">
          <span className="cart-toast-subtotal">
            Cart subtotal ({itemCount} item{itemCount === 1 ? "" : "s"})
          </span>
          <span className="cart-toast-subtotal-amount">${total.toFixed(2)}</span>
        </div>

        <div className="cart-toast-actions">
          <button className="cart-toast-view-btn" onClick={handleViewCart}>
            View cart
          </button>
          <button className="cart-toast-checkout-btn" onClick={handleCheckout}>
            Checkout
          </button>
        </div>
      </div>
    </div>
  );
}
