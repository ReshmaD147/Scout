import { createElement, useState } from "react";
import { Link } from "react-router-dom";

import { useCart } from "../context/CartContext";
import { useSavedItems } from "../context/SavedItemsContext";
import {
  formatCurrency,
  getAvailabilityPresentation,
  getImagePresentation,
  getPromotionPresentation,
} from "./ProductCard.helpers";
import { formatProductDisplayName } from "./productDisplay";
import "./ProductCard.css";

const API_BASE = import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8000";

export default function ProductCard({ product }) {
  const { addToCart } = useCart();
  const { toggleSave, isSaved } = useSavedItems();

  // Add-to-cart interaction state: "idle" | "loading" | "added" | "error" | "out_of_stock"
  const [cartState, setCartState] = useState("idle");
  const [imageFailed, setImageFailed] = useState(false);

  const isExternal = product.source === "external";
  const saved = isSaved(product);
  const promotionPresentation = getPromotionPresentation(product);
  const imagePresentation = getImagePresentation(product, imageFailed);
  const availabilityPresentation = getAvailabilityPresentation(product);
  const displayName = formatProductDisplayName(product.name);

  const resolvedClickUrl = product.click_url
    ? (product.click_url.startsWith("/") ? `${API_BASE}${product.click_url}` : product.click_url)
    : "#";

  async function handleAddToCart() {
    if (cartState === "loading") return;
    setCartState("loading");
    try {
      // addToCart now calls the real, authoritative /cart/add endpoint —
      // it validates stock and returns the true price server-side.
      // CartContext also guards against duplicate simultaneous requests
      // for the same product.
      await addToCart(product);
      setCartState("added");
      setTimeout(() => setCartState("idle"), 1800);
    } catch (err) {
      if (err.message === "Out of stock") {
        setCartState("out_of_stock");
      } else {
        setCartState("error");
        setTimeout(() => setCartState("idle"), 4000);
      }
    }
  }

  const externalLink = createElement(
    "div",
    { className: "product-card-external-wrap" },
    createElement(
      "a",
      {
        className: "product-card-btn product-card-btn--external",
        href: resolvedClickUrl,
        target: "_blank",
        rel: "noopener noreferrer sponsored",
        "aria-label": `View ${displayName} at ${product.vendor_name}`,
      },
      `View at ${product.vendor_name}`
    ),
    createElement(
      "p",
      { className: "product-card-affiliate-disclosure" },
      "Affiliate link"
    )
  );

  function renderCartButton() {
    if (cartState === "out_of_stock") {
      return (
        <button className="product-card-btn product-card-btn--disabled" disabled>
          Out of stock
        </button>
      );
    }

    if (cartState === "loading") {
      return (
        <button className="product-card-btn product-card-btn--loading" disabled>
          <span className="product-card-spinner" aria-hidden="true" />
          Adding…
        </button>
      );
    }

    if (cartState === "added") {
      return (
        <button className="product-card-btn product-card-btn--added" disabled>
          ✓ Added to cart
        </button>
      );
    }

    if (cartState === "error") {
      return (
        <>
          <button className="product-card-btn product-card-btn--error" onClick={handleAddToCart}>
            Try again
          </button>
          <p className="product-card-error-message">
            Failed to add item. Please try again.
          </p>
        </>
      );
    }

    return (
      <button className="product-card-btn" onClick={handleAddToCart}>
        Add to cart
      </button>
    );
  }

  return (
    <div className={`product-card product-card--${product.category || "default"}`}>
      <div className="product-card-image">
        {!imagePresentation.showPlaceholder ? (
          <img
            src={imagePresentation.imageUrl}
            alt=""
            aria-hidden="true"
            onError={() => setImageFailed(true)}
          />
        ) : (
          <div
            className={`product-card-image-placeholder product-card-image-placeholder--${isExternal ? "external" : product.category || "default"}`}
            role="img"
            aria-label={imagePresentation.placeholderAltText}
          >
            <span className="product-card-image-placeholder-icon" aria-hidden="true">
              ✦
            </span>
          </div>
        )}

        {promotionPresentation.badgeText && (
          <span className="product-card-badge product-card-badge--sale">
            {promotionPresentation.badgeText}
          </span>
        )}
        {isExternal && !promotionPresentation.badgeText && (
          <span className="product-card-badge">Third-party</span>
        )}

        <button
          className={`product-card-save ${saved ? "product-card-save--active" : ""}`}
          onClick={() => toggleSave(product)}
          aria-label={saved ? "Remove from saved items" : "Save item"}
        >
          {saved ? "♥" : "♡"}
        </button>
      </div>

      <div className="product-card-body">
        <p className="product-card-brand">{product.brand || product.vendor_name}</p>

        {product.product_id ? (
          <Link to={`/product/${product.product_id}`} className="product-card-name-link">
            <p className="product-card-name">{displayName}</p>
          </Link>
        ) : (
          <p className="product-card-name">{displayName}</p>
        )}

        {typeof product.rating === "number" && product.rating > 0 && (
          <div className="product-card-rating">
            <span className="product-card-star">★</span>
            <span>{product.rating.toFixed(1)}</span>
          </div>
        )}

        {promotionPresentation.hasPromotion && promotionPresentation.salePrice ? (
          <div className="product-card-price-row">
            <span className="product-card-price product-card-price--original">
              {formatCurrency(promotionPresentation.originalPrice)}
            </span>
            <span className="product-card-price product-card-price--sale">
              {formatCurrency(promotionPresentation.salePrice)}
            </span>
          </div>
        ) : (
          <p className="product-card-price">{formatCurrency(product.price)}</p>
        )}

        {!isExternal && (
          <p className={`product-card-availability product-card-availability--${availabilityPresentation.status}`}>
            <span className="product-card-availability-dot" />
            {availabilityPresentation.text}
          </p>
        )}

        <div className="product-card-action">
          {isExternal ? externalLink : renderCartButton()}
        </div>
      </div>
    </div>
  );
}
