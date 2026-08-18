import { useState, useRef, useEffect } from "react";
import { Link, useLocation } from "react-router-dom";
import { sendChatFeedback, streamChatMessage } from "../api/client";
import { useChatWidget } from "../context/ChatWidgetContext";
import { useCart } from "../context/CartContext";
import { useAuth } from "../context/AuthContext";
import {
  formatCurrency,
  getImagePresentation,
  getPromotionPresentation,
} from "./ProductCard.helpers";
import ReactMarkdown from "react-markdown";
import "./ChatWidget.css";

// Small inline icons matching the reference design, one per starter prompt
function ShoeIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M2 15c0-1.5 1-2.5 2.5-3l6-2.3c1-.4 1.7-1.3 1.8-2.4L12.5 5c.2-1.5 1.5-2 2.5-1l1 1c.6.6 1.4 1 2.3 1H20a2 2 0 0 1 2 2v3.5c0 2-1.5 3.5-3.5 3.5H4c-1.1 0-2-.9-2-2z" />
    </svg>
  );
}

function DressIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M9 3h6l1 3-2 2 3 12H7L10 8 8 6z" />
    </svg>
  );
}

function StoreIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M3 9l1.5-5h15L21 9" />
      <path d="M4 9v10a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1V9" />
      <path d="M9 20v-6h6v6" />
    </svg>
  );
}

function CompareIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M8 3L4 7l4 4" />
      <path d="M4 7h16" />
      <path d="M16 21l4-4-4-4" />
      <path d="M20 17H4" />
    </svg>
  );
}

function FeedbackIcon({ direction }) {
  const isPositive = direction === "up";
  return (
    <svg
      width="15"
      height="15"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {isPositive ? (
        <>
          <path d="M7 10v10" />
          <path d="M7 10l4.2-7c.5-.8 1.8-.5 1.8.5V8h5.4a2 2 0 0 1 1.9 2.5l-1.7 7A2 2 0 0 1 16.7 19H7" />
          <path d="M3 10h4v10H3z" />
        </>
      ) : (
        <>
          <path d="M7 14V4" />
          <path d="M7 14l4.2 7c.5.8 1.8.5 1.8-.5V16h5.4a2 2 0 0 0 1.9-2.5l-1.7-7A2 2 0 0 0 16.7 5H7" />
          <path d="M3 4h4v10H3z" />
        </>
      )}
    </svg>
  );
}

const STARTER_PROMPTS = [
  { text: "Recommend a dress under $80", Icon: DressIcon },
  { text: "Is the black midi dress in a medium?", Icon: StoreIcon },
  { text: "Where is order O1001?", Icon: CompareIcon },
  {
    text: "Recommend a black dress under $80, check Maple Grove medium availability, and explain opened-item returns.",
    Icon: DressIcon,
  },
  { text: "Do you have any red cocktail dresses under $50?", Icon: ShoeIcon },
];

// Maps real backend progress event values to human-readable labels.
// Only includes steps the backend genuinely emits — nothing invented.
const PROGRESS_LABELS = {
  understanding_request: "Understanding request",
  searching_products: "Searching products",
  checking_inventory: "Checking inventory",
  verifying_claims: "Verifying claims",
  preparing_response: "Preparing response",
};

const API_BASE = import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8000";

function resolveExternalUrl(url) {
  if (!url || typeof url !== "string") return "#";
  return url.startsWith("/") ? `${API_BASE}${url}` : url;
}

function productKey(product) {
  return product.product_id || product.external_product_id || product.name;
}

function ChatProductRail({ products, onInternalProductClick }) {
  const title = products.some((product) => product.source === "external")
    ? "Outside options"
    : "Recommended for you";

  return (
    <section className="chat-widget-product-section" aria-label={title}>
      <h3 className="chat-widget-product-section-title">{title}</h3>
      <div className="chat-widget-product-rail">
        {products.map((product) => (
          <ChatProductCard
            key={productKey(product)}
            product={product}
            onInternalProductClick={onInternalProductClick}
          />
        ))}
      </div>
    </section>
  );
}

function ChatProductCard({ product, onInternalProductClick }) {
  const [imageFailed, setImageFailed] = useState(false);
  const [addState, setAddState] = useState("idle"); // idle | adding | added
  const { addToCart } = useCart();
  const imagePresentation = getImagePresentation(product, imageFailed);
  const promotionPresentation = getPromotionPresentation(product);
  const isExternal = product.source === "external";

  const handleAddToCart = async (event) => {
    event.preventDefault();
    event.stopPropagation();
    if (addState !== "idle") return;
    setAddState("adding");
    try {
      await addToCart(product, 1, {
        recommendation_id: product.recommendation_id,
      });
      setAddState("added");
      setTimeout(() => setAddState("idle"), 2000);
    } catch (e) {
      console.warn("Failed to add to cart from chat:", e);
      setAddState("idle");
    }
  };
  const destination = isExternal
    ? resolveExternalUrl(product.click_url)
    : `/product/${product.product_id}`;
  const price = promotionPresentation.salePrice || product.price;
  const cardContent = (
    <>
      <div className="chat-widget-product-image">
        {!imagePresentation.showPlaceholder ? (
          <img
            src={imagePresentation.imageUrl}
            alt={imagePresentation.altText}
            onError={() => setImageFailed(true)}
          />
        ) : (
          <span className="chat-widget-product-placeholder" aria-hidden="true">
            ✦
          </span>
        )}
      </div>
      <div className="chat-widget-product-copy">
        <p className="chat-widget-product-brand">
          {isExternal ? product.vendor_name : product.brand || "Our catalog"}
        </p>
        <p className="chat-widget-product-name">{product.name}</p>
        <div className="chat-widget-product-price-row">
          {promotionPresentation.hasPromotion && promotionPresentation.originalPrice && (
            <span className="chat-widget-product-price chat-widget-product-price--original">
              {formatCurrency(promotionPresentation.originalPrice)}
            </span>
          )}
          {price !== undefined && price !== null && (
            <span className={`chat-widget-product-price ${promotionPresentation.salePrice ? "chat-widget-product-price--sale" : ""}`}>
              {formatCurrency(price)}
            </span>
          )}
        </div>
        {typeof product.rating === "number" && product.rating > 0 && (
          <p className="chat-widget-product-rating">★ {product.rating.toFixed(1)}</p>
        )}
        <span className="chat-widget-product-cta">
          {isExternal ? `View at ${product.vendor_name}` : "View details"}
        </span>
        {isExternal && <span className="chat-widget-product-affiliate">Affiliate link</span>}
        {!isExternal && (
          <button
            type="button"
            className="chat-widget-product-add-btn"
            onClick={handleAddToCart}
            disabled={addState !== "idle"}
          >
            {addState === "adding" ? "Adding…" : addState === "added" ? "Added ✓" : "Add to Cart"}
          </button>
        )}
      </div>
    </>
  );

  if (isExternal) {
    return (
      <a
        className="chat-widget-product-card"
        href={destination}
        target="_blank"
        rel="noopener noreferrer sponsored"
        aria-label={`View ${product.name} at ${product.vendor_name}`}
      >
        {cardContent}
      </a>
    );
  }

  return (
    <Link
      className="chat-widget-product-card"
      to={destination}
      onClick={onInternalProductClick}
      aria-label={`View details for ${product.name}`}
    >
      {cardContent}
    </Link>
  );
}

function ChatRefinementPrompt({ products, onPromptClick, disabled }) {
  const prompts = refinementPrompts(products);
  if (!prompts.length) return null;

  return (
    <div className="chat-widget-refine">
      <p>Are any of these catching your eye, or would you like to refine the search?</p>
      <div className="chat-widget-refine-chips">
        {prompts.map((prompt) => (
          <button
            key={prompt}
            type="button"
            className="chat-widget-refine-chip"
            onClick={() => onPromptClick(prompt)}
            disabled={disabled}
          >
            ✦ {prompt}
          </button>
        ))}
      </div>
    </div>
  );
}

function ChatQuickActions({ actions, onPromptClick, disabled }) {
  if (!actions.length) return null;

  return (
    <div className="chat-widget-quick-actions" aria-label="Suggested next actions">
      {actions.map((action) => (
        <button
          key={action}
          type="button"
          className="chat-widget-quick-action"
          onClick={() => onPromptClick(action)}
          disabled={disabled}
        >
          {action}
        </button>
      ))}
    </div>
  );
}

function refinementPrompts(products) {
  if (!products?.length) return [];
  const external = products.some((product) => product.source === "external");
  if (external) {
    return ["Show Scout options only", "Lower price", "Similar styles"];
  }

  const names = products.map((product) => String(product.name || "").toLowerCase()).join(" ");
  if (names.includes("dress")) {
    return ["Black dresses"];
  }
  if (names.includes("shoe") || names.includes("boot") || names.includes("sneaker")) {
    return ["Waterproof options"];
  }
  return ["Similar styles"];
}

export default function ChatWidget() {
  const {
    isOpen,
    setIsOpen,
    prefillMessage,
    prefillNonce,
    chatSessionId,
    setChatSessionId,
  } = useChatWidget();
  const { sessionId: authSessionId } = useAuth();
  const { addValidatedCartItem } = useCart();
  const location = useLocation();
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  // Use the authenticated session_id once the customer signs in (so
  // order/shipment questions are correctly recognized as coming from a
  // signed-in customer); otherwise fall back to whatever local session
  // the chat itself has already established.
  const sessionId = authSessionId || chatSessionId;
  const setSessionId = setChatSessionId;
  const [isLoading, setIsLoading] = useState(false);
  const [progressSteps, setProgressSteps] = useState([]);
  const [progressCollapsed, setProgressCollapsed] = useState(false);
  const [connectionError, setConnectionError] = useState(false);
  const [lastFailedMessage, setLastFailedMessage] = useState(null);
  const [messageFeedback, setMessageFeedback] = useState({});
  const messagesEndRef = useRef(null);
  const panelRef = useRef(null);
  const isProductPage = location.pathname.startsWith("/product/");
  const isHomePage = location.pathname === "/";

  useEffect(() => {
    if (isOpen) {
      messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
    }
  }, [messages, isOpen, progressSteps]);

  useEffect(() => {
    if (prefillMessage) {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setInput(prefillMessage);
    }
  }, [prefillNonce]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!isOpen) return;
    function handleKeyDown(e) {
      if (e.key === "Escape") {
        setIsOpen(false);
      }
    }
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [isOpen, setIsOpen]);

  async function runSend(messageText) {
    setIsLoading(true);
    setProgressSteps([]);
    setProgressCollapsed(false);
    setConnectionError(false);
    setLastFailedMessage(null);

    let gotAnyEvent = false;

    try {
      await streamChatMessage(messageText, sessionId, {
        onSession: (id) => {
          gotAnyEvent = true;
          setSessionId(id);
        },
        onProgress: (label) => {
          gotAnyEvent = true;
          setProgressSteps((prev) => [...prev, label]);
        },
        onDone: (reply, products, cartItem) => {
          gotAnyEvent = true;
          if (cartItem) {
            addValidatedCartItem(cartItem);
          }
          setMessages((prev) => [
            ...prev,
            {
              role: "assistant",
              content: reply,
              products: products || [],
              userMessage: messageText,
            },
          ]);
          setProgressSteps([]);
        },
        onError: () => {
          gotAnyEvent = true;
          setMessages((prev) => [
            ...prev,
            { role: "error", content: "Something went wrong reaching Scout. Try again." },
          ]);
          setLastFailedMessage(messageText);
        },
      });

      if (!gotAnyEvent) {
        setConnectionError(true);
        setLastFailedMessage(messageText);
      }
    } catch (err) {
      console.error("ChatWidget stream error:", err);
      setConnectionError(true);
      setLastFailedMessage(messageText);
    } finally {
      setIsLoading(false);
      setProgressSteps([]);
    }
  }

  async function handleSend(overrideText) {
    const trimmed = (overrideText ?? input).trim();
    if (!trimmed || isLoading) return;

    setMessages((prev) => [...prev, { role: "user", content: trimmed }]);
    setInput("");
    await runSend(trimmed);
  }

  function handleRetry() {
    if (!lastFailedMessage || isLoading) return;
    runSend(lastFailedMessage);
  }

  function handleStarterClick(prompt) {
    handleSend(prompt);
  }

  function handleClearChat() {
    setMessages([]);
    setMessageFeedback({});
    setProgressSteps([]);
    setConnectionError(false);
    setLastFailedMessage(null);
    setInput("");
  }

  function handleKeyDown(e) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  }

  function handleFeedback(messageIndex, value) {
    setMessageFeedback((current) => ({
      ...current,
      [messageIndex]: current[messageIndex] === value ? null : value,
    }));

    const message = messages[messageIndex];
    if (!sessionId || message?.role !== "assistant") {
      return;
    }

    sendChatFeedback({
      session_id: sessionId,
      message_index: messageIndex,
      rating: value,
      user_message: message.userMessage || "",
      assistant_reply: message.content || "",
      products: message.products || [],
    }).catch((error) => {
      console.error("Chat feedback error:", error);
    });
  }

  if (!isOpen) {
    return (
      <button
        className="chat-widget-bubble"
        onClick={() => setIsOpen(true)}
        aria-label="Open shopping assistant"
      >
        <svg
          className="chat-widget-bubble-icon"
          width="26"
          height="26"
          viewBox="0 0 24 24"
          fill="none"
          aria-hidden="true"
        >
          <path
            d="M5.5 17.5c-1.35-1.25-2-2.92-2-5 0-4.42 3.8-8 8.5-8s8.5 3.58 8.5 8-3.8 8-8.5 8c-1.05 0-2.05-.18-2.96-.52L5 20.5l.5-3z"
            stroke="currentColor"
            strokeWidth="1.8"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
          <path
            d="M8.5 12.25h.01M12 12.25h.01M15.5 12.25h.01"
            stroke="currentColor"
            strokeWidth="2.4"
            strokeLinecap="round"
          />
        </svg>
      </button>
    );
  }

  return (
    <div
      className={[
        "chat-widget-panel",
        isHomePage ? "chat-widget-panel--home-drawer" : "",
        isProductPage ? "chat-widget-panel--product-page" : "",
      ].filter(Boolean).join(" ")}
      ref={panelRef}
      role="dialog"
      aria-label="Scout shopping assistant"
    >
      <header className="chat-widget-header">
        <div className="chat-widget-header-titles">
          <span className="chat-widget-title">
            Ask Scout
          </span>
        </div>
        <div className="chat-widget-header-actions">
          <button
            className="chat-widget-clear"
            onClick={handleClearChat}
            disabled={isLoading && progressSteps.length > 0}
          >
            Clear chat
          </button>
          <button
            className="chat-widget-minimize"
            onClick={() => setIsOpen(false)}
            aria-label="Minimize"
          >
            −
          </button>
        </div>
      </header>

      <div className="chat-widget-messages">
        {messages.length === 0 && (
          <div className="chat-widget-empty">
            <p className="chat-widget-empty-title">Hi, I’m Scout.</p>
            <p className="chat-widget-empty-subtitle">Ask me naturally about products, availability, orders, or returns.</p>

            <div className="chat-widget-starters">
              {STARTER_PROMPTS.map(({ text, Icon }) => (
                <button
                  key={text}
                  className="chat-widget-starter-btn"
                  onClick={() => handleStarterClick(text)}
                  disabled={isLoading}
                >
                  <Icon />
                  <span>{text}</span>
                </button>
              ))}
            </div>

            <div className="chat-widget-capability-card">
              <span className="chat-widget-capability-kicker">What Scout can help with</span>
              <div className="chat-widget-capability-grid">
                <span>✓ Product search</span>
                <span>✓ Store availability</span>
                <span>✓ Order lookup</span>
                <span>✓ Returns policy</span>
              </div>
            </div>
          </div>
        )}

        {messages.map((msg, i) => {
          // Inventory quick-action buttons (nearby stores / online
          // delivery / find similar) intentionally removed - kept
          // buildInventoryQuickActions itself in the helpers file in
          // case this is revisited later, just no longer rendered here.
          const quickActions = [];

          return (
            <div key={i}>
              <div className={`chat-widget-bubble-msg chat-widget-bubble-msg--${msg.role}`}>
                {msg.role === "assistant" ? (
                  <ReactMarkdown
                    components={{
                      a: (props) => <a {...props} target="_blank" rel="noopener noreferrer" />,
                      p: (props) => <p style={{ margin: 0 }} {...props} />,
                    }}
                  >
                    {msg.content}
                  </ReactMarkdown>
                ) : (
                  msg.content
                )}
              </div>

              {msg.role === "assistant" && (
                <div className="chat-widget-feedback" aria-label="Rate this response">
                  <button
                    type="button"
                    className={`chat-widget-feedback-btn ${messageFeedback[i] === "up" ? "chat-widget-feedback-btn--active" : ""}`}
                    onClick={() => handleFeedback(i, "up")}
                    aria-label="Helpful response"
                    aria-pressed={messageFeedback[i] === "up"}
                  >
                    <FeedbackIcon direction="up" />
                  </button>
                  <button
                    type="button"
                    className={`chat-widget-feedback-btn ${messageFeedback[i] === "down" ? "chat-widget-feedback-btn--active" : ""}`}
                    onClick={() => handleFeedback(i, "down")}
                    aria-label="Not helpful response"
                    aria-pressed={messageFeedback[i] === "down"}
                  >
                    <FeedbackIcon direction="down" />
                  </button>
                </div>
              )}

              {msg.role === "error" && lastFailedMessage && i === messages.length - 1 && (
                <button className="chat-widget-retry-btn" onClick={handleRetry} disabled={isLoading}>
                  Retry
                </button>
              )}

              <ChatQuickActions
                actions={quickActions}
                onPromptClick={handleStarterClick}
                disabled={isLoading}
              />

              {msg.products && msg.products.length > 0 && (
                <>
                  <ChatProductRail
                    products={msg.products}
                    onInternalProductClick={() => setIsOpen(false)}
                  />
                  <ChatRefinementPrompt
                    products={msg.products}
                    onPromptClick={handleStarterClick}
                    disabled={isLoading}
                  />
                </>
              )}
            </div>
          );
        })}

        {isLoading && progressSteps.length > 0 && (
          <div className="chat-widget-progress">
            <button
              className="chat-widget-progress-toggle"
              onClick={() => setProgressCollapsed((c) => !c)}
              aria-expanded={!progressCollapsed}
            >
              <span className="chat-widget-progress-current">
                {PROGRESS_LABELS[progressSteps[progressSteps.length - 1]] || "Working…"}
              </span>
              <span className="chat-widget-progress-caret">{progressCollapsed ? "▸" : "▾"}</span>
            </button>
            {!progressCollapsed && (
              <ul className="chat-widget-progress-list">
                {progressSteps.map((step, idx) => (
                  <li key={idx} className="chat-widget-progress-item">
                    {PROGRESS_LABELS[step] || step}
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}

        {isLoading && progressSteps.length === 0 && (
          <div className="chat-widget-bubble-msg chat-widget-bubble-msg--assistant chat-widget-loading">
            <span className="chat-widget-dot" />
            <span className="chat-widget-dot" />
            <span className="chat-widget-dot" />
          </div>
        )}

        {connectionError && (
          <div className="chat-widget-disconnected">
            <p>Can't reach Scout right now. Check your connection and try again.</p>
            <button className="chat-widget-retry-btn" onClick={handleRetry}>
              Retry
            </button>
          </div>
        )}

        <div ref={messagesEndRef} />
      </div>

      <div className="chat-widget-input-area">
        <div className="chat-widget-input-row">
          <textarea
            className="chat-widget-input"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="Ask Scout's AI Shopping Assistant.."
            rows={1}
          />
          <button
            className="chat-widget-send"
            onClick={() => handleSend()}
            disabled={isLoading || !input.trim()}
          >
            Send
          </button>
        </div>
        <p className="chat-widget-disclaimer">
          Scout verifies product facts against store data before replying.
        </p>
      </div>
    </div>
  );
}
