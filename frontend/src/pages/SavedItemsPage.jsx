import { Link } from "react-router-dom";
import { useSavedItems } from "../context/SavedItemsContext";
import ProductCard from "../components/ProductCard";

export default function SavedItemsPage() {
  const { items } = useSavedItems();

  if (items.length === 0) {
    return (
      <div className="cart-empty">
        <p>No saved items yet.</p>
        <Link to="/" className="cart-empty-link">Continue shopping</Link>
      </div>
    );
  }

  return (
    <div>
      <h2 style={{ fontSize: "16px", marginBottom: "16px" }}>Saved Items</h2>
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(180px, 220px))",
          gap: "16px",
        }}
      >
        {items.map((p) => (
          <ProductCard key={p.product_id || p.external_product_id} product={p} />
        ))}
      </div>
    </div>
  );
}
