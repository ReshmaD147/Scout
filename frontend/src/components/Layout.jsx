import { useState } from "react";
import { Link, Outlet, useNavigate, useLocation } from "react-router-dom";
import { useCart } from "../context/CartContext";
import { useSavedItems } from "../context/SavedItemsContext";
import { useChatWidget } from "../context/ChatWidgetContext";
import ChatWidget from "./ChatWidget";
import CartToast from "./CartToast";
import "./Layout.css";

const CATEGORIES = ["Dresses", "Shoes", "Outerwear", "Tops", "Bottoms", "Accessories", "Sale"];

function ChatIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z" />
    </svg>
  );
}

function HeartIcon({ filled }) {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill={filled ? "currentColor" : "none"} stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.6l-1-1a5.5 5.5 0 0 0-7.8 7.8l1 1L12 21l7.8-7.6 1-1a5.5 5.5 0 0 0 0-7.8z" />
    </svg>
  );
}

function UserIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="8" r="4" />
      <path d="M4 21a8 8 0 0 1 16 0" />
    </svg>
  );
}

function CartIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="9" cy="21" r="1" />
      <circle cx="19" cy="21" r="1" />
      <path d="M2.5 3h2l2.6 12.4a2 2 0 0 0 2 1.6h8.4a2 2 0 0 0 2-1.6L21 8H6" />
    </svg>
  );
}

// New: concise line icons for the trust-badge row
function TruckIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <rect x="1" y="6" width="14" height="11" rx="1" />
      <path d="M15 9h4l3 3.5V17h-7z" />
      <circle cx="6" cy="19" r="1.6" />
      <circle cx="17.5" cy="19" r="1.6" />
    </svg>
  );
}

function ReturnIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M3 9a9 9 0 1 1 2.6 6.4" />
      <path d="M3 4v5h5" />
    </svg>
  );
}

function ShieldIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 3l7 3v5c0 4.5-3 8.2-7 10-4-1.8-7-5.5-7-10V6z" />
      <path d="M9.5 12l1.8 1.8L14.8 10" />
    </svg>
  );
}

export default function Layout() {
  const { itemCount, total } = useCart();
  const { count: savedCount } = useSavedItems();
  const { isOpen: isChatOpen, setIsOpen: setChatOpen } = useChatWidget();
  const [searchInput, setSearchInput] = useState("");
  const navigate = useNavigate();
  const location = useLocation();

  function handleSearchSubmit(e) {
    e.preventDefault();
    const trimmed = searchInput.trim();
    if (trimmed) {
      navigate(`/search?q=${encodeURIComponent(trimmed)}`);
    }
  }

  function isCategoryActive(catPath) {
    return location.pathname === catPath;
  }

  const isProductPage = location.pathname.startsWith("/product/");
  const layoutClasses = [
    "layout",
    isProductPage && isChatOpen ? "layout--product-chat-open" : "",
  ].filter(Boolean).join(" ");

  return (
    <div className={layoutClasses}>
      <header className="layout-header">
        <Link to="/" className="layout-logo">
          <span className="layout-logo-mark" aria-hidden="true">
            <svg
              width="22"
              height="22"
              viewBox="0 0 24 24"
              fill="none"
              xmlns="http://www.w3.org/2000/svg"
            >
              {/* Lumi Burst */}

              <circle
                cx="12"
                cy="12"
                r="2.2"
                fill="currentColor"
              />

              <path
                d="M12 4V7"
                stroke="currentColor"
                strokeWidth="1.8"
                strokeLinecap="round"
              />

              <path
                d="M12 17V20"
                stroke="currentColor"
                strokeWidth="1.8"
                strokeLinecap="round"
              />

              <path
                d="M5.1 8L7.7 9.5"
                stroke="currentColor"
                strokeWidth="1.8"
                strokeLinecap="round"
              />

              <path
                d="M16.3 14.5L18.9 16"
                stroke="currentColor"
                strokeWidth="1.8"
                strokeLinecap="round"
              />

              <path
                d="M5.1 16L7.7 14.5"
                stroke="currentColor"
                strokeWidth="1.8"
                strokeLinecap="round"
              />

              <path
                d="M16.3 9.5L18.9 8"
                stroke="currentColor"
                strokeWidth="1.8"
                strokeLinecap="round"
              />
            </svg>
          </span>
          <span className="layout-logo-text">Lumi</span>
        </Link>

        <form className="layout-search" onSubmit={handleSearchSubmit} role="search">
          <span className="layout-search-icon">⌕</span>
          <input
            placeholder="Search dresses, shoes, brands, or occasions..."
            value={searchInput}
            onChange={(e) => setSearchInput(e.target.value)}
            aria-label="Search dresses, shoes, brands, or occasions"
          />
        </form>

        <div className="layout-header-actions">
          <Link to="/saved" className="layout-action-btn" aria-label={`Saved items (${savedCount})`}>
            <span className="layout-action-icon">
              <HeartIcon />
              {savedCount > 0 && <span className="layout-badge">{savedCount}</span>}
            </span>
            <span className="layout-action-label">Saved</span>
          </Link>

          <div className="layout-account-wrap">
            <Link
              className="layout-action-btn"
              aria-label="Account page"
              to="/account"
            >
              <span className="layout-action-icon">
                <UserIcon />
              </span>
              <span className="layout-action-label">Account</span>
            </Link>
          </div>

          <button
            className="layout-help-btn"
            onClick={() => setChatOpen(true)}
            aria-label="Ask Scout shopping assistant"
          >
            <ChatIcon />
            <span className="layout-action-label">Ask Scout</span>
          </button>

          <Link to="/cart" className="layout-cart-btn" aria-label={`Cart, ${itemCount} items, subtotal $${total.toFixed(2)}`}>
            <span className="layout-action-icon">
              <CartIcon />
              {itemCount > 0 && <span className="layout-badge">{itemCount}</span>}
            </span>
            <span className="layout-cart-text">
              <span className="layout-cart-label">Cart · {itemCount}</span>
              {itemCount > 0 && (
                <span className="layout-cart-subtotal">${total.toFixed(2)}</span>
              )}
            </span>
          </Link>
        </div>
      </header>

      <nav className="layout-nav">
        <div className="layout-nav-categories">
          <Link
            to="/"
            className={`layout-nav-link ${isCategoryActive("/") ? "layout-nav-link--active" : ""}`}
          >
            All
          </Link>
          {CATEGORIES.map((cat) => {
            const path = `/category/${cat.toLowerCase()}`;
            const isSale = cat === "Sale";
            const active = isCategoryActive(path);
            const classNames = [
              "layout-nav-link",
              active ? "layout-nav-link--active" : "",
              isSale ? "layout-nav-link--sale" : "",
            ].filter(Boolean).join(" ");
            return (
              <Link key={cat} to={path} className={classNames}>
                {cat}
              </Link>
            );
          })}
        </div>

        {/* New: retail trust badges — static marketing copy, no backend data source */}
        <div className="layout-trust-badges">
          <div className="layout-trust-item">
            <TruckIcon />
            <span className="layout-trust-text">
              <span className="layout-trust-label">Free shipping</span>
              <span className="layout-trust-desc">on orders $75+</span>
            </span>
          </div>
          <div className="layout-trust-item">
            <ReturnIcon />
            <span className="layout-trust-text">
              <span className="layout-trust-label">Easy returns</span>
              <span className="layout-trust-desc">30-day returns</span>
            </span>
          </div>
          <div className="layout-trust-item">
            <ShieldIcon />
            <span className="layout-trust-text">
              <span className="layout-trust-label">Secure checkout</span>
              <span className="layout-trust-desc">SSL encrypted</span>
            </span>
          </div>
        </div>
      </nav>

      <main className="layout-content">
        <Outlet />
      </main>

      <ChatWidget />
      <CartToast />
    </div>
  );
}
