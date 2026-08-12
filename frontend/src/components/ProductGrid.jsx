import { useEffect, useState } from "react";
import ProductCard from "./ProductCard";
import "./ProductGrid.css";

const API_BASE = import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8000";

export default function ProductGrid({ category = null, query = null, limit = 50 }) {
  const [products, setProducts] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setLoading(true);
    const params = new URLSearchParams({ limit: String(limit) });
    if (category) params.set("category", category);
    if (query) params.set("query", query);

    fetch(`${API_BASE}/products?${params}`)
      .then((r) => r.json())
      .then((data) => setProducts(data))
      .finally(() => setLoading(false));
  }, [category, query, limit]);

  if (loading) return <p className="product-grid-status">Loading…</p>;
  if (products.length === 0) return <p className="product-grid-status">No products found.</p>;

  return (
    <div className="product-grid">
      {products.map((p) => (
        <ProductCard key={p.product_id} product={p} />
      ))}
    </div>
  );
}
