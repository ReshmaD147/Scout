import { Link } from "react-router-dom";
import ProductGrid from "../components/ProductGrid";
import { useChatWidget } from "../context/ChatWidgetContext";
import "./HomePage.css";

export default function HomePage() {
  const { isOpen: isChatOpen, openChatWithMessage } = useChatWidget();

  return (
    <div className="home-page">
      <section className="home-hero" aria-labelledby="home-hero-title">
        <div className="home-hero-copy">
          <p className="home-hero-kicker">AI-verified shopping</p>
          <h1 id="home-hero-title" className="home-hero-title">
            Find the right look faster — with facts checked before you buy.
          </h1>
          <p className="home-hero-subtitle">
            Scout helps compare products, check availability, and keep checkout safely in the storefront.
          </p>
          <div className="home-hero-actions">
            <Link to="/category/dresses" className="home-hero-btn">
              Shop dresses
            </Link>
            <button
              className="home-hero-secondary"
              type="button"
              onClick={() => openChatWithMessage("Recommend a dress under $80")}
            >
              Ask Scout
            </button>
          </div>
          <div className="home-hero-trust" aria-label="Scout safety features">
            <span>Verified product facts</span>
            <span>Read-only AI tools</span>
            <span>Secure checkout</span>
          </div>
        </div>
        <div
          className={`home-hero-prompt-card ${isChatOpen ? "home-hero-prompt-card--hidden" : ""}`}
          aria-hidden="true"
        >
          <span className="home-hero-prompt-kicker">Try Scout</span>
          <strong>“Is this available in medium?”</strong>
          <span>Verified stock answers before checkout.</span>
        </div>

      </section>

      <div className="home-section-header">
        <h2 className="home-section-title">Featured products</h2>
        <Link to="/search" className="home-view-all-link">
          View all products →
        </Link>
      </div>
      <ProductGrid limit={12} />
    </div>
  );
}
