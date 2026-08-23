import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  formatCurrency,
  getImagePresentation,
  getPromotionPresentation,
} from "../components/ProductCard.helpers";
import { formatProductDisplayName } from "../components/productDisplay";
import { getAccountSummary } from "../api/client";
import { useAuth } from "../context/AuthContext";
import { useSavedItems } from "../context/SavedItemsContext";
import "./AccountPage.css";

function formatMoney(value) {
  return `$${Number(value || 0).toFixed(2)}`;
}

function formatDate(value) {
  if (!value) return "Unknown date";
  return new Date(value).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}

function formatAddress(address) {
  if (!address) return "No shipping address saved";
  return [
    address.full_name,
    address.address_line1,
    address.address_line2,
    [address.city, address.state, address.postal_code].filter(Boolean).join(", "),
    address.country,
  ].filter(Boolean).join(" · ") || "No shipping address saved";
}

function formatCustomerName(customer) {
  const rawName = String(customer?.name || "").trim();
  if (rawName) {
    return rawName
      .replace(/([a-z])([A-Z])/g, "$1 $2")
      .replace(/([A-Za-z])(\d+)/g, "$1 $2")
      .replace(/\s+/g, " ");
  }
  const suffix = String(customer?.customer_id || "").match(/\d+$/)?.[0];
  return suffix ? `Demo Customer ${Number(suffix)}` : "Demo Customer";
}

function savedProductPrice(product) {
  const promotion = getPromotionPresentation(product);
  return promotion.hasPromotion && promotion.salePrice
    ? formatCurrency(promotion.salePrice)
    : formatCurrency(product.price);
}

function CompactSavedItem({ product }) {
  const [imageFailed, setImageFailed] = useState(false);
  const image = getImagePresentation(product, imageFailed);
  const productId = product.product_id || product.external_product_id;
  const displayName = formatProductDisplayName(product.name);
  const content = (
    <>
      <div className="account-saved-thumb">
        {!image.showPlaceholder ? (
          <img
            src={image.imageUrl}
            alt=""
            aria-hidden="true"
            onError={() => setImageFailed(true)}
          />
        ) : (
          <span aria-hidden="true">✦</span>
        )}
      </div>
      <div className="account-saved-info">
        <strong>{displayName}</strong>
        <span>{product.brand || product.vendor_name || "Lumi catalog"}</span>
      </div>
      <span className="account-saved-price">{savedProductPrice(product)}</span>
    </>
  );

  if (product.product_id) {
    return (
      <Link className="account-saved-item" to={`/product/${product.product_id}`}>
        {content}
      </Link>
    );
  }

  return (
    <div className="account-saved-item" key={productId}>
      {content}
    </div>
  );
}

export default function AccountPage() {
  const { customerId, customerName, sessionId, isSignedIn, signingIn, signInAsDemoCustomer, signOut } = useAuth();
  const { items: savedItems } = useSavedItems();
  const [summary, setSummary] = useState(null);
  const [expandedOrderId, setExpandedOrderId] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (!isSignedIn || !sessionId) {
      return;
    }
    let cancelled = false;
    Promise.resolve()
      .then(() => {
        if (!cancelled) {
          setLoading(true);
          setError(null);
        }
        return getAccountSummary(sessionId);
      })
      .then((data) => {
        if (!cancelled) setSummary(data);
      })
      .catch(() => {
        if (!cancelled) setError("We couldn’t load account details right now.");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [isSignedIn, sessionId]);

  if (!isSignedIn) {
    return (
      <div className="account-page account-page--centered">
        <section className="account-card account-signin-card">
          <div className="account-signin-icon" aria-hidden="true">👤</div>
          <div>
            <p className="account-eyebrow">My account</p>
            <h1>Sign in to view your Lumi account</h1>
            <p>
              See protected order history, shipments, saved items, and Scout
              recommendation feedback for this account.
            </p>
          </div>
          <button
            className="account-primary-btn"
            onClick={() => signInAsDemoCustomer("C001")}
            disabled={signingIn}
          >
            {signingIn ? "Signing in…" : "Sign in as Sarah Chen"}
          </button>
        </section>
      </div>
    );
  }

  const customer = summary?.customer || {
    customer_id: customerId,
    name: customerName || "Customer",
    email: `${String(customerId || "customer").toLowerCase()}@demo.lumi.local`,
    demo_identity: true,
  };
  const orders = summary?.orders || [];
  const feedback = summary?.recommendation_feedback || [];

  return (
    <div className="account-page">
      <section className="account-hero account-card">
        <div className="account-identity">
          <p className="account-eyebrow">My account</p>
          <h1>{formatCustomerName(customer)}</h1>
          <p>{customer.email}</p>
          <div className="account-identity-badges">
            <span className="account-protected-pill">Protected account</span>
          </div>
        </div>
        <button className="account-secondary-btn" onClick={signOut}>Sign out</button>
      </section>

      {error && <p className="account-error">{error}</p>}
      {loading && <p className="account-muted">Loading account details…</p>}

      <section className="account-grid">
        <div className="account-card account-section">
          <div className="account-section-header">
            <div>
              <p className="account-eyebrow">Orders</p>
              <h2>My orders</h2>
            </div>
            <span className="account-count-pill">{orders.length} {orders.length === 1 ? "order" : "orders"}</span>
          </div>

          {orders.length === 0 ? (
            <p className="account-muted">No orders yet for {customerName || "this account"}.</p>
          ) : (
            <div className="account-order-list">
              {orders.map((order) => {
                const expanded = expandedOrderId === order.order_id;
                return (
                  <article className="account-order" key={order.order_id}>
                    <button
                      className="account-order-summary"
                      onClick={() => setExpandedOrderId(expanded ? null : order.order_id)}
                      aria-expanded={expanded}
                    >
                      <span>
                        <strong>Order {order.order_id}</strong>
                        <small>{formatDate(order.created_at)} · {order.status}</small>
                      </span>
                      <span className="account-order-meta">
                        <strong>{formatMoney(order.total)}</strong>
                        <small>{expanded ? "Hide details" : "View details →"}</small>
                      </span>
                    </button>
                    {expanded && (
                      <div className="account-order-details">
                        <p><strong>Shipping</strong><span>{formatAddress(order.shipping_address)}</span></p>
                        <p>
                          <strong>Shipment</strong>
                          <span>
                            {order.shipment?.shipped
                              ? `${order.shipment.carrier} ${order.shipment.tracking_number} · ${order.shipment.status}`
                              : order.shipment?.message || "Shipment not created yet"}
                          </span>
                        </p>
                        <div className="account-order-items">
                          {order.items.map((item) => (
                            <p key={`${order.order_id}-${item.product_id}`}>
                              <span>{formatProductDisplayName(item.name)} × {item.quantity}</span>
                              <span>{formatMoney(item.price_at_purchase * item.quantity)}</span>
                            </p>
                          ))}
                        </div>
                      </div>
                    )}
                  </article>
                );
              })}
            </div>
          )}
        </div>

        <div className="account-card account-section">
          <div className="account-section-header">
            <div>
              <p className="account-eyebrow">Scout</p>
              <h2>Recommendation feedback</h2>
            </div>
            <span className="account-count-pill">{feedback.length}</span>
          </div>
          {feedback.length === 0 ? (
            <div className="account-empty-mini">
              <span aria-hidden="true">👍 👎</span>
              <p>No recommendation feedback yet. Use thumbs on Scout product cards.</p>
            </div>
          ) : (
            <div className="account-feedback-list">
              {feedback.map((item) => (
                <div className="account-feedback-item" key={item.feedback_id}>
                  <span className="account-feedback-icon">{item.rating === "up" ? "👍" : "👎"}</span>
                  <span>{formatProductDisplayName(item.product_name || item.product_id)}</span>
                  <small>{formatDate(item.updated_at)}</small>
                </div>
              ))}
            </div>
          )}
        </div>
      </section>

      <section className="account-card account-section">
        <div className="account-section-header">
          <div>
            <p className="account-eyebrow">Saved</p>
            <h2>Saved items</h2>
          </div>
          <Link to="/saved">View saved</Link>
        </div>
        {savedItems.length === 0 ? (
          <div className="account-empty-saved">
            <span aria-hidden="true">♡</span>
            <p>No saved items yet.</p>
            <Link to="/">Browse products</Link>
          </div>
        ) : (
          <div className="account-saved-grid">
            {savedItems.slice(0, 4).map((product) => (
              <CompactSavedItem key={product.product_id || product.external_product_id} product={product} />
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
